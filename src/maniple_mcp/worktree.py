"""
Git worktree utilities for worker session isolation.

Provides functions to create, remove, and list git worktrees, enabling
each worker session to operate in its own isolated working directory
while sharing the same repository history.

Two worktree strategies are supported:

1. External worktrees (legacy):
   ~/.maniple/worktrees/{repo-path-hash}/{worker-name}-{timestamp}/
   - Created outside the target repo to avoid polluting it
   - No .gitignore modifications needed

2. Local worktrees (preferred):
   {repo}/.worktrees/{issue-badge}/ or {name-uuid-badge}/
   - Kept within the repo for easier discovery and cleanup
   - Automatically adds .worktrees to .gitignore
"""

import hashlib
import os
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional


# Local worktree directory name within repos
LOCAL_WORKTREE_DIR = ".worktrees"

# Hard timeout (seconds) for every git subprocess call. Without this a hung git
# operation (network, locked index, unresponsive filesystem) would block the
# entire spawn/close pipeline indefinitely.
GIT_SUBPROCESS_TIMEOUT = 30


def slugify(text: str) -> str:
    """
    Convert text to a URL/filesystem-friendly slug.

    Converts to lowercase, replaces spaces and special chars with dashes,
    and removes consecutive dashes.

    Args:
        text: The text to slugify

    Returns:
        A lowercase, dash-separated string safe for filenames/URLs

    Example:
        slugify("Add local worktrees support")  # "add-local-worktrees-support"
        slugify("Fix Bug #123")                 # "fix-bug-123"
    """
    # Convert to lowercase
    text = text.lower()
    # Replace spaces and underscores with dashes
    text = re.sub(r"[\s_]+", "-", text)
    # Remove any characters that aren't alphanumeric or dashes
    text = re.sub(r"[^a-z0-9-]", "", text)
    # Collapse multiple dashes
    text = re.sub(r"-+", "-", text)
    # Strip leading/trailing dashes
    text = text.strip("-")
    return text


def short_slug(text: str, max_length: int = 30) -> str:
    """
    Create a slug suitable for compact identifiers.

    Truncates long slugs to keep branch and directory names short,
    while preserving the leading portion of the slug.
    """
    slug = slugify(text)
    if len(slug) <= max_length:
        return slug
    return slug[:max_length].rstrip("-")



def ensure_gitignore_entry(repo_path: Path, entry: str) -> bool:
    """
    Ensure an entry exists in the repository's .gitignore file.

    Creates the .gitignore file if it doesn't exist. Adds the entry
    on a new line if not already present.

    Args:
        repo_path: Path to the repository root
        entry: The gitignore entry to add (e.g., ".worktrees")

    Returns:
        True if the entry was added, False if it already existed

    Example:
        ensure_gitignore_entry(Path("/path/to/repo"), ".worktrees")
    """
    gitignore_path = Path(repo_path) / ".gitignore"

    # Check if entry already exists
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        lines = content.splitlines()

        # Check for exact match (with or without trailing slash)
        entry_variants = {entry, entry + "/", entry.rstrip("/")}
        for line in lines:
            stripped = line.strip()
            if stripped in entry_variants:
                return False

        # Entry not found, append it
        # Ensure there's a newline before our entry if file doesn't end with one
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"{entry}\n"
        gitignore_path.write_text(content)
        return True
    else:
        # Create new .gitignore with the entry
        gitignore_path.write_text(f"{entry}\n")
        return True


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""

    pass


def _run_git(
    args: list[str],
    *,
    timeout: int = GIT_SUBPROCESS_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """
    Run a git command with a hard timeout, capturing output.

    Centralizes the timeout so a hung git process can never block the caller
    indefinitely. A timeout is surfaced as a WorktreeError (the process is
    killed by subprocess.run before TimeoutExpired is raised).

    Args:
        args: Full argv (e.g. ["git", "-C", repo, "worktree", "list"]).
        timeout: Seconds before the process is killed.

    Returns:
        The completed process (caller inspects returncode/stdout/stderr).

    Raises:
        WorktreeError: If the command does not complete within ``timeout``.
    """
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise WorktreeError(
            f"git command timed out after {timeout}s: {' '.join(map(str, args))}"
        ) from e


def _safe_path_component(value: str, *, field: str) -> str:
    """
    Validate that ``value`` is safe to use as a single filesystem path component.

    Guards against path traversal: rejects values containing path separators,
    parent references (``..``), null bytes, or absolute paths. This prevents an
    attacker-influenced identifier (e.g. ``issue_id``) from escaping the
    intended ``.worktrees/`` directory.

    Args:
        value: The candidate path component.
        field: Name of the source field, used in error messages.

    Returns:
        The validated value (unchanged).

    Raises:
        WorktreeError: If the value is not a single safe path component.
    """
    if not value or value in (".", ".."):
        raise WorktreeError(f"Invalid {field}: {value!r} is not a usable path component")
    if "/" in value or "\\" in value or "\x00" in value:
        raise WorktreeError(
            f"Invalid {field}: {value!r} must not contain path separators"
        )
    # Path.name strips any directory portion; if it differs, the value was not a
    # plain single component (catches absolute paths and other surprises).
    if Path(value).is_absolute() or Path(value).name != value:
        raise WorktreeError(
            f"Invalid {field}: {value!r} must be a single path component"
        )
    return value


def _rmtree_at(dir_fd: int, name: str) -> None:
    """
    Recursively remove ``name`` located in the directory referenced by ``dir_fd``,
    without following symlinks at any level.

    Every operation is performed relative to a file descriptor opened with
    ``O_NOFOLLOW``/``O_DIRECTORY``, so once the top directory's inode is pinned a
    later swap of any ancestor *pathname* (a TOCTOU symlink race) cannot redirect
    the deletion to a different location. A symlink encountered as an entry is
    unlinked, never traversed.
    """
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        sub_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd
        )
        try:
            for entry in os.listdir(sub_fd):
                _rmtree_at(sub_fd, entry)
        finally:
            os.close(sub_fd)
        os.rmdir(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def remove_orphan_dir_safely(parent_dir: Path, name: str) -> None:
    """
    Delete a child directory ``name`` under ``parent_dir`` without ever following
    a symlink for ``parent_dir`` or any component below it.

    This is the TOCTOU-safe replacement for ``shutil.rmtree(parent_dir / name)``:
    ``parent_dir`` is opened with ``O_NOFOLLOW`` (raising if it is/became a
    symlink), pinning its real inode; the removal then runs relative to that
    descriptor so a parent-symlink swap cannot escape ``parent_dir``.

    Raises:
        WorktreeError: If ``parent_dir`` is a symlink, ``name`` is not a single
            path component, or the platform lacks ``dir_fd`` support.
    """
    if Path(name).name != name or name in (".", ".."):
        raise WorktreeError(f"Refusing to remove unsafe orphan name: {name!r}")
    # os.{stat,unlink,rmdir,open} use the dir_fd= parameter (supports_dir_fd);
    # os.listdir takes the fd directly as its path (supports_fd).
    if not {os.stat, os.unlink, os.rmdir, os.open}.issubset(os.supports_dir_fd) or (
        os.listdir not in os.supports_fd
    ):
        raise WorktreeError(
            "Platform lacks dir_fd support required for safe worktree removal"
        )
    try:
        parent_fd = os.open(
            parent_dir, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        )
    except OSError as e:
        raise WorktreeError(
            f"Refusing to remove orphan under {parent_dir}: {e}"
        ) from e
    try:
        _rmtree_at(parent_fd, name)
    finally:
        os.close(parent_fd)


def worktree_has_changes(worktree_path: Path) -> bool:
    """
    Return True if a worktree has uncommitted or untracked changes.

    Uses ``git status --porcelain`` scoped to the worktree. A non-empty result
    means there is work that would be lost if the worktree were force-removed.
    On any error (e.g. the directory is not a git worktree), returns True
    conservatively so callers default to preserving the directory.

    Args:
        worktree_path: Path to the worktree to inspect.

    Returns:
        True if the worktree is dirty (or its state cannot be determined),
        False only when git confirms a clean working tree.
    """
    worktree_path = Path(worktree_path).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def get_repo_hash(repo_path: Path) -> str:
    """
    Generate a short hash from a repository path.

    Used to create unique subdirectories for each repo's worktrees.

    Args:
        repo_path: Absolute path to the repository

    Returns:
        8-character hex hash of the repo path
    """
    return hashlib.sha256(str(repo_path).encode()).hexdigest()[:8]


def get_worktree_base_for_repo(repo_path: Path) -> Path:
    """
    Get the base directory for a repo's worktrees.

    Args:
        repo_path: Path to the main repository

    Returns:
        Path to ~/.maniple/worktrees/{repo-hash}/
    """
    repo_path = Path(repo_path).resolve()
    repo_hash = get_repo_hash(repo_path)
    from maniple.paths import resolve_data_dir

    return resolve_data_dir() / "worktrees" / repo_hash


def create_worktree(
    repo_path: Path,
    worktree_name: str,
    branch: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Path:
    """
    Create a git worktree for a worker.

    Creates a new worktree at:
        ~/.maniple/worktrees/{repo-hash}/{worktree_name}-{timestamp}/

    If a branch is specified and doesn't exist, it will be created from HEAD.
    If no branch is specified, creates a detached HEAD worktree.

    Args:
        repo_path: Path to the main repository
        worktree_name: Name for the worktree (worker name, e.g., "John-abc123")
        branch: Branch to checkout (creates new branch from HEAD if doesn't exist)
        timestamp: Unix timestamp for directory name (defaults to current time)

    Returns:
        Path to the created worktree

    Raises:
        WorktreeError: If the git worktree command fails

    Example:
        path = create_worktree(
            repo_path=Path("/path/to/repo"),
            worktree_name="John-abc123",
            branch="John-abc123"
        )
        # Returns: Path("~/.maniple/worktrees/a1b2c3d4/John-abc123-1703001234")
    """
    repo_path = Path(repo_path).resolve()

    # Generate worktree path outside the repo
    if timestamp is None:
        timestamp = int(time.time())
    worktree_dir_name = f"{worktree_name}-{timestamp}"
    base_dir = get_worktree_base_for_repo(repo_path)
    worktree_path = base_dir / worktree_dir_name

    # Ensure base directory exists
    base_dir.mkdir(parents=True, exist_ok=True)

    # Check if worktree already exists
    if worktree_path.exists():
        raise WorktreeError(f"Worktree already exists at {worktree_path}")

    # Build the git worktree add command
    cmd = ["git", "-C", str(repo_path), "worktree", "add"]

    if branch:
        # Check if branch exists
        branch_check = _run_git(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
        )

        if branch_check.returncode == 0:
            # Branch exists, check it out
            cmd.extend([str(worktree_path), branch])
        else:
            # Branch doesn't exist, create it with -b
            cmd.extend(["-b", branch, str(worktree_path)])
    else:
        # No branch specified, create detached HEAD
        cmd.extend(["--detach", str(worktree_path)])

    result = _run_git(cmd)

    if result.returncode != 0:
        raise WorktreeError(f"Failed to create worktree: {result.stderr.strip()}")

    return worktree_path


def _resolve_worktree_base(repo_path: Path, base: str) -> str:
    # Resolve base ref to a commit hash to avoid worktree-locked branch refs.
    def _rev_parse(ref: str) -> Optional[str]:
        result = _run_git(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", ref],
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    commit = _rev_parse(f"{base}^{{commit}}")
    if commit:
        return commit

    normalized_base = base.removeprefix("refs/heads/")
    try:
        for worktree in list_git_worktrees(repo_path):
            if worktree.get("branch") == normalized_base and worktree.get("commit"):
                return worktree["commit"]
    except WorktreeError:
        pass

    raise WorktreeError(
        f"Base ref not found: {base}. Ensure it exists locally or fetch it."
    )


def create_local_worktree(
    repo_path: Path,
    worker_name: str,
    issue_id: Optional[str] = None,
    badge: Optional[str] = None,
    branch: Optional[str] = None,
    base: Optional[str] = None,
) -> Path:
    """
    Create a git worktree in the repo's .worktrees/ directory.

    Creates a new worktree at:
        {repo}/.worktrees/{issue_id}-{badge}/  (if issue_id provided)
        {repo}/.worktrees/{worker_name}-{uuid}-{badge}/  (otherwise)

    The branch name matches the worktree directory name for consistency unless
    an explicit branch is provided.
    Automatically adds .worktrees to .gitignore if not present.

    If a generated worktree path or branch already exists, appends an
    incrementing suffix (-1, -2, etc.) until an available name is found.
    This allows multiple workers to work on the same issue in parallel.
    When an explicit branch is provided, pre-existing branches are treated
    as an error.

    Args:
        repo_path: Path to the main repository
        worker_name: Name of the worker (used in fallback naming)
        issue_id: Optional issue tracker ID (e.g., "cic-abc123")
        badge: Optional badge text for the worktree name
        branch: Optional branch name to create for the worktree
        base: Optional base ref/branch for the new branch

    Returns:
        Path to the created worktree

    Raises:
        WorktreeError: If the git worktree command fails

    Example:
        # With issue ID
        path = create_local_worktree(
            repo_path=Path("/path/to/repo"),
            worker_name="Groucho",
            issue_id="cic-abc",
            badge="Add local worktrees"
        )
        # Returns: Path("/path/to/repo/.worktrees/cic-abc-add-local-worktrees")

        # If called again with same issue/badge:
        # Returns: Path("/path/to/repo/.worktrees/cic-abc-add-local-worktrees-1")

        # Without issue ID
        path = create_local_worktree(
            repo_path=Path("/path/to/repo"),
            worker_name="Groucho",
            badge="Fix bug"
        )
        # Returns: Path("/path/to/repo/.worktrees/groucho-a1b2c3d4-fix-bug")
    """
    repo_path = Path(repo_path).resolve()

    # Build the worktree directory name
    if issue_id:
        # issue_id is used directly as (part of) a path component, so validate
        # it cannot contain separators or traversal sequences. badge is already
        # sanitized via short_slug().
        issue_id = _safe_path_component(issue_id, field="issue_id")
        # Issue-based naming: {issue_id}-{badge}
        if badge:
            dir_name = f"{issue_id}-{short_slug(badge)}"
        else:
            dir_name = issue_id
    else:
        # Fallback naming: {worker_name}-{uuid}-{badge}
        short_uuid = uuid.uuid4().hex[:8]
        name_slug = slugify(worker_name)
        if badge:
            dir_name = f"{name_slug}-{short_uuid}-{short_slug(badge)}"
        else:
            dir_name = f"{name_slug}-{short_uuid}"

    # Worktree path inside the repo
    worktrees_dir = repo_path / LOCAL_WORKTREE_DIR

    # Reject a symlinked .worktrees root. A symlink here would let the later
    # is_relative_to() containment check pass (both sides resolve through the
    # link) while git actually writes outside the repository, and would let
    # orphan cleanup rmtree() through the link. .worktrees must be a real dir.
    if worktrees_dir.is_symlink():
        raise WorktreeError(
            f".worktrees must be a real directory inside the repo, not a "
            f"symlink: {worktrees_dir}"
        )

    # Ensure .worktrees is in .gitignore
    ensure_gitignore_entry(repo_path, LOCAL_WORKTREE_DIR)

    # Ensure .worktrees directory exists
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Find an available name, handling collisions with incrementing suffix.
    # Check both path existence and branch existence (git won't allow the same
    # branch checked out in multiple worktrees).
    def branch_exists(name: str) -> bool:
        result = _run_git(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", f"refs/heads/{name}"],
        )
        return result.returncode == 0

    base_dir_name = dir_name
    worktree_path = worktrees_dir / dir_name
    suffix = 0

    if branch:
        if branch_exists(branch):
            raise WorktreeError(f"Branch already exists: {branch}")
        while worktree_path.exists():
            suffix += 1
            dir_name = f"{base_dir_name}-{suffix}"
            worktree_path = worktrees_dir / dir_name
        branch_name = branch
    else:
        while worktree_path.exists() or branch_exists(dir_name):
            suffix += 1
            dir_name = f"{base_dir_name}-{suffix}"
            worktree_path = worktrees_dir / dir_name
        branch_name = dir_name

    # Defense in depth: ensure the final path is still inside the repo before we
    # ask git to create anything. Anchored on the (already-resolved, real)
    # repo_path rather than worktrees_dir — resolving worktrees_dir would follow
    # a symlink swapped in for .worktrees and defeat the check.
    if not worktree_path.resolve().is_relative_to(repo_path):
        raise WorktreeError(
            f"Refusing to create worktree outside repo {repo_path}: {worktree_path}"
        )

    resolved_base = None
    if base:
        resolved_base = _resolve_worktree_base(repo_path, base)

    # Build the git worktree add command.
    # Branch is guaranteed not to exist (collision loop checked for it).
    cmd = ["git", "-C", str(repo_path), "worktree", "add", "-b", branch_name, str(worktree_path)]
    if resolved_base:
        cmd.append(resolved_base)

    result = _run_git(cmd)

    if result.returncode != 0:
        raise WorktreeError(f"Failed to create local worktree: {result.stderr.strip()}")

    # Post-creation containment: catch a symlink swapped in AFTER the is_symlink()
    # check but BEFORE git ran (TOCTOU). If the worktree landed outside the repo,
    # fail loudly. We deliberately do NOT attempt a path-based `git worktree
    # remove` rollback here: the swapped parent symlink would make that rollback
    # delete OUTSIDE the repo. Leaving a stray directory is far safer than
    # performing an out-of-repo deletion; the operator can remove it manually.
    if not worktree_path.resolve().is_relative_to(repo_path):
        raise WorktreeError(
            f"Worktree was created outside repo {repo_path} (symlink race?): "
            f"{worktree_path.resolve()}. Left in place; remove it manually."
        )

    return worktree_path


def remove_worktree(
    repo_path: Path,
    worktree_path: Path,
    force: bool = False,
) -> bool:
    """
    Remove a worktree directory (does NOT delete the branch).

    The branch is intentionally kept alive so that commits can be
    cherry-picked before manual cleanup.

    By default (``force=False``) git refuses to remove a worktree that has
    uncommitted or untracked changes, so callers must opt in to discarding
    work. Use :func:`worktree_has_changes` to check before forcing.

    Args:
        repo_path: Path to the main repository
        worktree_path: Full path to the worktree to remove
        force: If True, force removal even with uncommitted changes
            (DESTRUCTIVE — discards uncommitted work)

    Returns:
        True if worktree was successfully removed

    Raises:
        WorktreeError: If the git worktree remove command fails

    Example:
        success = remove_worktree(
            repo_path=Path("/path/to/repo"),
            worktree_path=Path("~/.maniple/worktrees/a1b2c3d4/John-abc123-1703001234")
        )
    """
    repo_path = Path(repo_path).resolve()
    worktree_path = Path(worktree_path).resolve()

    cmd = ["git", "-C", str(repo_path), "worktree", "remove"]

    if force:
        cmd.append("--force")

    cmd.append(str(worktree_path))

    result = _run_git(cmd)

    if result.returncode != 0:
        # Check if worktree doesn't exist (not an error)
        if "is not a working tree" in result.stderr or "No such file" in result.stderr:
            return True
        raise WorktreeError(f"Failed to remove worktree: {result.stderr.strip()}")

    # Also run prune to clean up stale worktree references. Best-effort: a prune
    # timeout must not fail an already-successful removal.
    try:
        _run_git(["git", "-C", str(repo_path), "worktree", "prune"])
    except WorktreeError:
        pass

    return True


def list_git_worktrees(repo_path: Path) -> list[dict]:
    """
    List all worktrees registered with git for a repository.

    Parses the porcelain output of git worktree list to provide
    structured information about each worktree.

    Args:
        repo_path: Path to the repository

    Returns:
        List of dicts, each containing:
            - path: Path to the worktree
            - branch: Branch name (or None if detached HEAD)
            - commit: Current HEAD commit hash
            - bare: True if this is the bare repository entry
            - detached: True if HEAD is detached

    Raises:
        WorktreeError: If the git worktree list command fails

    Example:
        worktrees = list_git_worktrees(Path("/path/to/repo"))
        for wt in worktrees:
            print(f"{wt['path']}: {wt['branch'] or 'detached'}")
    """
    repo_path = Path(repo_path).resolve()

    result = _run_git(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
    )

    if result.returncode != 0:
        raise WorktreeError(f"Failed to list worktrees: {result.stderr.strip()}")

    worktrees = []
    current_worktree: dict = {}

    for line in result.stdout.strip().split("\n"):
        if not line:
            # Empty line separates worktree entries
            if current_worktree:
                worktrees.append(current_worktree)
                current_worktree = {}
            continue

        if line.startswith("worktree "):
            current_worktree["path"] = Path(line[9:])
            current_worktree["branch"] = None
            current_worktree["commit"] = None
            current_worktree["bare"] = False
            current_worktree["detached"] = False
        elif line.startswith("HEAD "):
            current_worktree["commit"] = line[5:]
        elif line.startswith("branch "):
            # Branch is in format "refs/heads/branch-name"
            branch_ref = line[7:]
            if branch_ref.startswith("refs/heads/"):
                current_worktree["branch"] = branch_ref[11:]
            else:
                current_worktree["branch"] = branch_ref
        elif line == "bare":
            current_worktree["bare"] = True
        elif line == "detached":
            current_worktree["detached"] = True

    # Don't forget the last entry
    if current_worktree:
        worktrees.append(current_worktree)

    return worktrees


def list_local_worktrees(repo_path: Path) -> list[dict]:
    """
    List all local worktrees in a repository's .worktrees/ directory.

    Finds worktrees in {repo}/.worktrees/ and cross-references them
    with git's worktree list to determine registration status.

    Args:
        repo_path: Path to the repository

    Returns:
        List of dicts, each containing:
            - path: Path to the worktree directory
            - name: Worktree directory name (e.g., "cic-abc-fix-bug")
            - branch: Branch name (if found in git worktree list)
            - commit: Current HEAD commit hash (if found)
            - registered: True if git knows about this worktree
            - exists: True if the directory exists on disk

    Example:
        worktrees = list_local_worktrees(Path("/path/to/repo"))
        for wt in worktrees:
            status = "active" if wt["registered"] else "orphaned"
            print(f"{wt['name']}: {status}")
    """
    repo_path = Path(repo_path).resolve()
    local_worktrees_dir = repo_path / LOCAL_WORKTREE_DIR

    # Get git's view of worktrees
    try:
        git_worktrees = list_git_worktrees(repo_path)
    except WorktreeError:
        git_worktrees = []

    git_worktree_paths = {str(wt["path"]) for wt in git_worktrees}

    worktrees = []

    # Check if .worktrees directory exists
    if not local_worktrees_dir.exists():
        return worktrees

    # Scan the directory for worktree folders
    for item in local_worktrees_dir.iterdir():
        if not item.is_dir():
            continue

        wt_path_str = str(item.resolve())
        registered = wt_path_str in git_worktree_paths

        # Find matching git worktree info if registered
        git_info = None
        for gwt in git_worktrees:
            if str(gwt["path"]) == wt_path_str:
                git_info = gwt
                break

        worktrees.append({
            "path": item,
            "name": item.name,
            "branch": git_info["branch"] if git_info else None,
            "commit": git_info["commit"] if git_info else None,
            "registered": registered,
            "exists": True,
        })

    return worktrees
