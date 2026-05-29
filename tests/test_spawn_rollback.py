"""Tests for spawn_workers resource rollback on partial failure (C2/H4).

A failed spawn must leave no orphaned panes, worktrees, or registry entries.
"""

import subprocess
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

import maniple_mcp.session_state as session_state
from maniple_mcp.registry import SessionRegistry
from maniple_mcp.terminal_backends.base import TerminalSession
from maniple_mcp.tools import spawn_workers as spawn_workers_module


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class RollbackBackend:
    """tmux-like backend whose agent start fails, recording closed panes."""

    backend_id = "tmux"

    def __init__(self):
        self.sessions = []
        self.closed = []

    async def create_session(self, name=None, *, project_path=None, issue_id=None,
                             coordinator_badge=None, profile=None,
                             profile_customizations=None):
        session = TerminalSession(
            backend_id=self.backend_id,
            native_id=f"%{len(self.sessions)}",
            handle=None,
        )
        self.sessions.append(session)
        return session

    async def start_agent_in_session(self, **kwargs):
        raise RuntimeError("simulated agent startup failure")

    async def close_session(self, session, force=False):
        self.closed.append((session, force))


class LateFailBackend:
    """Backend where the agent starts (and dirties its worktree) but a LATER
    step (marker/prompt send) fails — exercising the data-safety rollback."""

    backend_id = "tmux"

    def __init__(self):
        self.sessions = []
        self.closed = []

    async def create_session(self, name=None, *, project_path=None, issue_id=None,
                             coordinator_badge=None, profile=None,
                             profile_customizations=None):
        session = TerminalSession(
            backend_id=self.backend_id,
            native_id=f"%{len(self.sessions)}",
            handle=None,
        )
        self.sessions.append(session)
        return session

    async def start_agent_in_session(self, *, project_path=None, **kwargs):
        # Simulate the worker beginning work: leave an uncommitted change in
        # its worktree. This must NOT be discarded by a later-failure rollback.
        from pathlib import Path
        (Path(project_path) / "work-in-progress.txt").write_text("precious\n")

    async def send_prompt_for_agent(self, *args, **kwargs):
        raise RuntimeError("simulated late failure during prompt send")

    async def close_session(self, session, force=False):
        self.closed.append((session, force))


@pytest.mark.asyncio
async def test_spawn_rollback_cleans_panes_worktrees_registry(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda a: f"cli:{a}")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *a, **k: "MARKER")

    backend = RollbackBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [
            {"project_path": str(repo), "name": "Worker1", "use_worktree": True},
            {"project_path": str(repo), "name": "Worker2", "use_worktree": True},
        ],
    }, context=ctx)

    # Spawn failed.
    assert "error" in result

    # Registry left empty (no orphaned entries).
    assert registry.count() == 0

    # Both created panes were force-closed.
    assert len(backend.closed) == len(backend.sessions) == 2
    assert all(force is True for _, force in backend.closed)

    # Both worktrees were removed from .worktrees/.
    worktrees_dir = repo / ".worktrees"
    leftover = [p for p in worktrees_dir.iterdir() if p.is_dir()] if worktrees_dir.exists() else []
    assert leftover == [], f"worktrees not cleaned up: {leftover}"


@pytest.mark.asyncio
async def test_spawn_explicit_worktree_failure_rolls_back_prior(tmp_path, monkeypatch):
    """Worker 2's explicit-worktree failure must roll back worker 1's worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda a: f"cli:{a}")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)

    real_create = spawn_workers_module.create_local_worktree
    calls = {"n": 0}

    def flaky_create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_create(*args, **kwargs)  # worker 1 succeeds
        from maniple_mcp.worktree import WorktreeError
        raise WorktreeError("simulated worktree failure")  # worker 2 fails

    monkeypatch.setattr(spawn_workers_module, "create_local_worktree", flaky_create)

    backend = RollbackBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [
            {"project_path": str(repo), "name": "Worker1", "worktree": {}},
            {"project_path": str(repo), "name": "Worker2", "worktree": {}},
        ],
    }, context=ctx)

    assert "error" in result
    assert registry.count() == 0
    worktrees_dir = repo / ".worktrees"
    leftover = [p for p in worktrees_dir.iterdir() if p.is_dir()] if worktrees_dir.exists() else []
    assert leftover == [], f"prior worktree not rolled back: {leftover}"


@pytest.mark.asyncio
async def test_late_failure_preserves_dirty_worktree(tmp_path, monkeypatch):
    """A failure AFTER a worker started must NOT discard its uncommitted work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    monkeypatch.setattr(spawn_workers_module, "get_cli_backend", lambda a: f"cli:{a}")
    monkeypatch.setattr(spawn_workers_module, "get_worktree_tracker_dir", lambda *_: None)
    monkeypatch.setattr(session_state, "generate_marker_message", lambda *a, **k: "MARKER")

    backend = LateFailBackend()
    registry = SessionRegistry()
    app_ctx = SimpleNamespace(registry=registry, backend=backend)

    async def ensure_connection(app_context):
        return app_context.backend

    mcp = FastMCP("test")
    spawn_workers_module.register_tools(mcp, ensure_connection)
    tool = mcp._tool_manager.get_tool("spawn_workers")

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_ctx))
    result = await tool.run({
        "workers": [{"project_path": str(repo), "name": "Worker1", "use_worktree": True}],
    }, context=ctx)

    # Spawn failed and registry was cleaned up...
    assert "error" in result
    assert registry.count() == 0

    # ...but the worktree with uncommitted work was PRESERVED (not force-removed).
    worktrees = [p for p in (repo / ".worktrees").iterdir() if p.is_dir()]
    assert len(worktrees) == 1, "dirty worktree should be preserved on late failure"
    assert (worktrees[0] / "work-in-progress.txt").read_text() == "precious\n"
