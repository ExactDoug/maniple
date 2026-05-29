"""Tests for the worktree module."""

import subprocess

import pytest

from maniple_mcp.worktree import (
    WorktreeError,
    _safe_path_component,
    create_local_worktree,
    remove_orphan_dir_safely,
    remove_worktree,
    short_slug,
    worktree_has_changes,
)


def _init_repo(path):
    """Create a minimal git repo with one commit at ``path``."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class TestSafePathComponent:
    """MAN-SEC-002: issue_id / path components must not enable traversal."""

    @pytest.mark.parametrize(
        "bad",
        ["../escape", "a/b", "a\\b", "..", ".", "", "/abs/path", "x\x00y"],
    )
    def test_rejects_unsafe_values(self, bad):
        with pytest.raises(WorktreeError):
            _safe_path_component(bad, field="issue_id")

    @pytest.mark.parametrize("good", ["cic-abc123", "ISSUE-42", "worker_1"])
    def test_accepts_safe_values(self, good):
        assert _safe_path_component(good, field="issue_id") == good


class TestCreateLocalWorktreeTraversal:
    """MAN-SEC-002: a malicious issue_id must not escape .worktrees/."""

    def test_traversal_issue_id_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        with pytest.raises(WorktreeError):
            create_local_worktree(
                repo_path=repo,
                worker_name="Groucho",
                issue_id="../../../../tmp/pwned",
                branch="feature",
            )
        # Nothing should have been created outside the repo.
        assert not (tmp_path / "pwned").exists()

    def test_valid_issue_id_creates_inside_worktrees(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt = create_local_worktree(
            repo_path=repo, worker_name="Groucho", issue_id="cic-abc"
        )
        assert wt.resolve().is_relative_to((repo / ".worktrees").resolve())

    def test_symlinked_worktrees_root_rejected(self, tmp_path):
        """A symlinked .worktrees root must be refused (escape via symlink)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".worktrees").symlink_to(outside, target_is_directory=True)
        with pytest.raises(WorktreeError):
            create_local_worktree(
                repo_path=repo, worker_name="Groucho", issue_id="cic-abc"
            )
        # Nothing should have been written into the symlink target.
        assert list(outside.iterdir()) == []


class TestWorktreeRemovalSafety:
    """C3: removal must default to non-force and respect dirty state."""

    def test_remove_worktree_defaults_to_non_force(self):
        import inspect

        sig = inspect.signature(remove_worktree)
        assert sig.parameters["force"].default is False

    def test_worktree_has_changes_detects_dirty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt = create_local_worktree(
            repo_path=repo, worker_name="Groucho", issue_id="cic-dirty"
        )
        assert worktree_has_changes(wt) is False
        (wt / "scratch.txt").write_text("uncommitted work\n")
        assert worktree_has_changes(wt) is True

    def test_non_force_removal_refuses_dirty_worktree(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt = create_local_worktree(
            repo_path=repo, worker_name="Groucho", issue_id="cic-keep"
        )
        (wt / "scratch.txt").write_text("uncommitted work\n")
        # Default (force=False) must NOT delete a dirty worktree.
        with pytest.raises(WorktreeError):
            remove_worktree(repo_path=repo, worktree_path=wt)
        assert wt.exists()
        # Forced removal discards it.
        assert remove_worktree(repo_path=repo, worktree_path=wt, force=True) is True
        assert not wt.exists()


class TestRemoveOrphanDirSafely:
    """
    GAP 2 (TOCTOU): orphan deletion must never follow a symlinked parent out of
    the repo. remove_orphan_dir_safely pins the parent inode via O_NOFOLLOW.
    """

    def test_removes_clean_child_dir(self, tmp_path):
        parent = tmp_path / ".worktrees"
        parent.mkdir()
        child = parent / "orphan"
        child.mkdir()
        (child / "f.txt").write_text("x")
        remove_orphan_dir_safely(parent, "orphan")
        assert not child.exists()
        assert parent.exists()

    def test_refuses_when_parent_is_symlink(self, tmp_path):
        """If .worktrees is a symlink, removal must refuse and not touch target."""
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "orphan"
        victim.mkdir()
        (victim / "important.txt").write_text("do not delete")

        link = tmp_path / ".worktrees"
        link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(WorktreeError):
            remove_orphan_dir_safely(link, "orphan")
        # The symlink target must be untouched.
        assert victim.exists()
        assert (victim / "important.txt").read_text() == "do not delete"

    def test_rejects_unsafe_name(self, tmp_path):
        parent = tmp_path / ".worktrees"
        parent.mkdir()
        for bad in ["../escape", "a/b", ".."]:
            with pytest.raises(WorktreeError):
                remove_orphan_dir_safely(parent, bad)


class TestShortSlug:
    """Tests for short_slug function."""

    def test_empty_string_returns_empty(self):
        """Empty input should return empty string."""
        assert short_slug("") == ""

    def test_all_special_chars_returns_empty(self):
        """All special chars should slugify to empty string."""
        assert short_slug("!!!@@@") == ""

    def test_exact_max_length_passthrough(self):
        """Input matching max length should not be truncated."""
        text = "a" * 30
        assert short_slug(text) == text

    def test_truncation_strips_trailing_hyphen(self):
        """Truncated slug should not end with a hyphen."""
        text = ("a" * 29) + "-" + "b"
        assert short_slug(text) == "a" * 29

    def test_shorter_than_max_passthrough(self):
        """Short input should return slugified text unchanged."""
        assert short_slug("Short Slug") == "short-slug"
