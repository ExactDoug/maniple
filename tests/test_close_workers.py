"""Tool-level tests for close_workers worktree data-loss safety.

Covers the guard that a worktree with uncommitted/untracked changes is
PRESERVED on close unless force=True, and removed when clean.
"""

import subprocess
from types import SimpleNamespace

import pytest

from maniple_mcp.registry import SessionStatus
from maniple_mcp.tools import close_workers as close_workers_module
from maniple_mcp.worktree import create_local_worktree


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class FakeBackend:
    """No-op terminal backend capturing close calls."""

    def __init__(self):
        self.closed = []

    async def send_key(self, session, key):
        pass

    async def send_text(self, session, text):
        pass

    async def close_session(self, session, force=False):
        self.closed.append((session, force))


class FakeRegistry:
    def __init__(self):
        self.removed = []

    def remove(self, sid):
        self.removed.append(sid)


def _make_session(worktree_path, repo):
    return SimpleNamespace(
        status=SessionStatus.READY,
        agent_type="claude",
        terminal_session=object(),
        worktree_path=worktree_path,
        main_repo_path=repo,
    )


@pytest.mark.asyncio
async def test_dirty_worktree_preserved_without_force(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt = create_local_worktree(repo_path=repo, worker_name="Groucho", issue_id="cic-1")
    (wt / "scratch.txt").write_text("uncommitted\n")  # make it dirty

    backend = FakeBackend()
    registry = FakeRegistry()
    session = _make_session(wt, repo)

    result = await close_workers_module._close_single_worker(
        backend, session, "cic-1", registry, force=False
    )

    assert result["success"] is True
    assert result["worktree_cleaned"] is False
    assert result["worktree_preserved"] is True
    assert result["worktree_path"] == str(wt)
    assert wt.exists()  # not deleted


@pytest.mark.asyncio
async def test_dirty_worktree_removed_with_force(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt = create_local_worktree(repo_path=repo, worker_name="Harpo", issue_id="cic-2")
    (wt / "scratch.txt").write_text("uncommitted\n")

    backend = FakeBackend()
    registry = FakeRegistry()
    session = _make_session(wt, repo)

    result = await close_workers_module._close_single_worker(
        backend, session, "cic-2", registry, force=True
    )

    assert result["success"] is True
    assert result["worktree_cleaned"] is True
    assert "worktree_preserved" not in result
    assert not wt.exists()  # force discarded it


@pytest.mark.asyncio
async def test_clean_worktree_removed_without_force(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt = create_local_worktree(repo_path=repo, worker_name="Chico", issue_id="cic-3")
    # leave clean

    backend = FakeBackend()
    registry = FakeRegistry()
    session = _make_session(wt, repo)

    result = await close_workers_module._close_single_worker(
        backend, session, "cic-3", registry, force=False
    )

    assert result["success"] is True
    assert result["worktree_cleaned"] is True
    assert not wt.exists()
