"""
List worktrees tool.

Provides list_worktrees for managing claude-team created git worktrees.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

if TYPE_CHECKING:
    from ..server import AppContext

from ..worktree import (
    list_local_worktrees,
    remove_orphan_dir_safely,
    worktree_has_changes,
    WorktreeError,
    LOCAL_WORKTREE_DIR,
)
from ..utils import error_response, HINTS

logger = logging.getLogger("maniple")


def register_tools(mcp: FastMCP) -> None:
    """Register list_worktrees tool on the MCP server."""

    @mcp.tool()
    async def list_worktrees(
        ctx: Context[ServerSession, "AppContext"],
        repo_path: str,
        remove_orphans: bool | None = False,
    ) -> dict:
        """
        List worktrees in a repository's .worktrees/ directory.

        Shows all worktrees created by claude-team for the specified repository,
        including which are orphaned (directory exists but not registered with git).

        Args:
            repo_path: Path to the repository to list worktrees for
            remove_orphans: If True, remove orphaned worktrees that are not
                registered with git. For safety, an orphan is removed ONLY if it
                has a .git marker AND a clean working tree; orphans with
                uncommitted/untracked changes are preserved and reported via
                `removal_skipped`.

        Returns:
            Dict with:
                - repo_path: The repository path
                - worktrees_dir: Path to .worktrees/ directory
                - worktrees: List of worktree info dicts containing:
                    - path: Full path to worktree
                    - name: Directory name (e.g., "cic-abc-fix-bug")
                    - branch: Git branch (if registered)
                    - commit: Current commit (if registered)
                    - registered: True if git knows about this worktree
                    - removed: True if orphan was removed (when remove_orphans=True)
                    - removal_skipped: Reason string if an orphan was preserved
                      ("uncommitted-changes" or "not-a-worktree")
                - total: Total number of worktrees
                - orphan_count: Number of orphaned worktrees
                - removed_count: Number of orphans removed (when remove_orphans=True)
                - skipped_count: Number of orphans preserved for safety
        """
        # Handle None values from MCP clients that send explicit null for omitted params
        remove_orphans = remove_orphans if remove_orphans is not None else False

        resolved_path = Path(repo_path).resolve()
        if not resolved_path.exists():
            return error_response(
                f"Repository path does not exist: {repo_path}",
                hint=HINTS["project_path_missing"],
            )

        worktrees_dir = resolved_path / LOCAL_WORKTREE_DIR

        # Refuse to remove orphans through a symlinked .worktrees root: rmtree
        # would delete the symlink's target (outside the repo). Listing is still
        # safe, so only block the destructive path.
        if remove_orphans and worktrees_dir.is_symlink():
            return error_response(
                f".worktrees is a symlink; refusing to remove orphans through it: "
                f"{worktrees_dir}",
                hint="Remove or replace the .worktrees symlink with a real directory.",
            )

        worktrees = list_local_worktrees(resolved_path)

        result_worktrees = []
        orphan_count = 0
        removed_count = 0

        skipped_count = 0

        for wt in worktrees:
            wt_info = {
                "path": str(wt["path"]),
                "name": wt["name"],
                "branch": wt["branch"],
                "commit": wt["commit"],
                "registered": wt["registered"],
                "removed": False,
            }

            if not wt["registered"]:
                orphan_count += 1
                if remove_orphans:
                    orphan_path = Path(wt["path"])
                    # Safety guards before an irreversible delete:
                    # 0) The resolved target MUST stay inside the repo. Anchored
                    #    on resolved_path (a real path) this defeats a symlink
                    #    swapped into .worktrees after the earlier is_symlink()
                    #    check (TOCTOU) — rmtree can never escape the repo.
                    # 1) Only delete things that are actually git worktrees
                    #    (have a .git marker). Never rmtree an arbitrary dir.
                    # 2) Never delete a worktree that has uncommitted/untracked
                    #    changes — that work would be lost permanently.
                    if not orphan_path.resolve().is_relative_to(resolved_path):
                        wt_info["removal_skipped"] = "escapes-repo"
                        skipped_count += 1
                        logger.warning(
                            f"Skipping orphan removal for {orphan_path}: "
                            f"resolves outside repo {resolved_path} (symlink?)"
                        )
                    elif not (orphan_path / ".git").exists():
                        wt_info["removal_skipped"] = "not-a-worktree"
                        skipped_count += 1
                        logger.warning(
                            f"Skipping orphan removal for {orphan_path}: "
                            "no .git marker (not a recognizable worktree)"
                        )
                    elif worktree_has_changes(orphan_path):
                        wt_info["removal_skipped"] = "uncommitted-changes"
                        skipped_count += 1
                        logger.warning(
                            f"Skipping orphan removal for {orphan_path}: "
                            "has uncommitted or untracked changes"
                        )
                    else:
                        try:
                            # Remove the orphaned (clean) worktree directory using
                            # an fd-pinned, symlink-refusing delete so a TOCTOU
                            # parent-symlink swap cannot redirect rmtree outside
                            # the repo.
                            remove_orphan_dir_safely(worktrees_dir, wt["name"])
                            wt_info["removed"] = True
                            removed_count += 1
                            logger.info(f"Removed orphaned worktree: {orphan_path}")
                        except WorktreeError as e:
                            wt_info["removal_skipped"] = "unsafe-path"
                            skipped_count += 1
                            logger.warning(
                                f"Refusing unsafe orphan removal for {orphan_path}: {e}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to remove orphaned worktree {orphan_path}: {e}"
                            )

            result_worktrees.append(wt_info)

        return {
            "repo_path": str(resolved_path),
            "worktrees_dir": str(worktrees_dir),
            "worktrees": result_worktrees,
            "total": len(worktrees),
            "orphan_count": orphan_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
        }
