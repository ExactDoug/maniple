# Maniple Security Review

Date: 2026-05-28

Reviewer: OpenAI Codex

## 1. Executive summary

I reviewed `src/`, `scripts/`, and `commands/` with focus on command/prompt injection, path traversal, subprocess safety, untrusted worker output, secrets handling, and worker-to-manager escape paths.

The repository is generally disciplined about using argument-vector subprocess calls instead of `shell=True`, and most filesystem helpers normalize paths before use. I did not find evidence of direct secrets storage in the repository code, and the Claude JSONL recovery path is notably more defensive than the Codex equivalent.

I did find two real security issues:

1. A shell-command injection path in worker launch code. Unquoted values are concatenated into a command string and then pasted into an interactive shell.
2. A worktree path traversal issue. `issue_id` is used directly in the worktree directory name, which can escape the intended `.worktrees/` directory when combined with an explicit branch.

## 2. Findings table

| ID | Title | Severity | File:line |
| --- | --- | --- | --- |
| MAN-SEC-001 | Worker launch path builds shell commands from unquoted input | High | `src/maniple_mcp/cli_backends/base.py:131`, `src/maniple_mcp/iterm_utils.py:694`, `src/maniple_mcp/terminal_backends/tmux.py:503` |
| MAN-SEC-002 | Local worktree creation trusts `issue_id` as a path component | Medium | `src/maniple_mcp/worktree.py:332`, `src/maniple_mcp/worktree.py:368`, `src/maniple_mcp/worktree.py:392` |

## 3. Per-finding detail

### MAN-SEC-001: Worker launch path builds shell commands from unquoted input

Severity: High

Description:

The worker launch path constructs a single shell command string and sends it into an existing terminal session. Several values that can originate from tool input, config, or environment are interpolated without shell escaping:

- `project_path`
- CLI command override values
- `plugin_dir`
- environment variable values prepended via `env_vars`
- optional `output_capture_path`

Because these values are concatenated directly into shell syntax, an attacker who can influence them can execute arbitrary shell commands in the manager-owned terminal session before the worker starts.

Impact:

- Arbitrary command execution with the manager session's local privileges.
- Direct bypass of intended worker isolation/sandboxing.
- Potential credential exposure if the injected command reads local secrets or alters the environment before launching the worker.

Evidence:

`src/maniple_mcp/cli_backends/base.py:131`

```python
if args:
    cmd = f"{cmd} {' '.join(args)}"

if env_vars:
    env_exports = " ".join(f"{k}={v}" for k, v in env_vars.items())
    cmd = f"{env_exports} {cmd}"
```

`src/maniple_mcp/iterm_utils.py:694`

```python
cmd = f"cd {project_path} && {agent_cmd}"
await send_prompt(session, cmd)
```

`src/maniple_mcp/terminal_backends/tmux.py:503`

```python
cmd = f"cd {project_path} && {agent_cmd}"
await self.send_prompt(handle, cmd, submit=True)
```

User-controlled values reach this path from `spawn_workers`, including `project_path` and `plugin_dir`:

`src/maniple_mcp/tools/spawn_workers.py:335`

```python
repo_path = Path(project_path).expanduser().resolve()
```

`src/maniple_mcp/tools/spawn_workers.py:686`

```python
plugin_dir = worker_config.get("plugin_dir")
```

Remediation:

- Stop building shell command strings with string concatenation.
- Prefer launching the CLI as a properly escaped argv sequence.
- If terminal backends must still send text to a shell, quote every interpolated token with `shlex.quote()` at minimum.
- Quote `project_path`, each CLI arg, `plugin_dir`, environment values, and `output_capture_path`.
- Reject command override values that contain shell metacharacters unless they are modeled as an explicit argv list.

### MAN-SEC-002: Local worktree creation trusts `issue_id` as a path component

Severity: Medium

Description:

When `issue_id` is present, `create_local_worktree()` uses it directly in the directory name without slugification or path-component validation. The resulting `worktree_path` is formed with `worktrees_dir / dir_name` and passed to `git worktree add`.

If a caller supplies an `issue_id` containing path separators, `..`, or an absolute path, the computed worktree path can point outside `{repo}/.worktrees/`. This is especially exploitable when the caller also supplies an explicit valid `worktree.branch`, because the branch name no longer has to match the unsafe `issue_id`.

Impact:

- Worktrees can be created outside the intended repository-local `.worktrees/` directory.
- A caller can cause writes in unexpected filesystem locations accessible to the current user.
- This weakens repo isolation guarantees and can interfere with unrelated directories.

Evidence:

`src/maniple_mcp/worktree.py:332`

```python
if issue_id:
    if badge:
        dir_name = f"{issue_id}-{short_slug(badge)}"
    else:
        dir_name = issue_id
```

`src/maniple_mcp/worktree.py:368`

```python
worktree_path = worktrees_dir / dir_name
```

`src/maniple_mcp/worktree.py:392`

```python
cmd = ["git", "-C", str(repo_path), "worktree", "add", "-b", branch_name, str(worktree_path)]
```

The unsafe value comes directly from tool input:

`src/maniple_mcp/tools/spawn_workers.py:348`

```python
issue_id=issue_id,
```

Remediation:

- Slugify `issue_id` before using it in a filesystem path.
- Alternatively, validate that `issue_id` is a single safe path component:
  - no `/` or `\\`
  - no `..`
  - not absolute
- After constructing `worktree_path`, resolve it and verify it remains under `worktrees_dir` before invoking `git worktree add`.
- Apply the same invariant to any future path-derived branch/worktree naming inputs.

## 4. Notes on what looked safe / good practices observed

- I did not find `shell=True` usage in the reviewed Python code. Most subprocesses use argument arrays, which is the right default, for example `tmux` execution in [src/maniple_mcp/terminal_backends/tmux.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/terminal_backends/tmux.py:551) and git worktree commands in [src/maniple_mcp/worktree.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/worktree.py:360).
- Worker-name and badge-derived worktree components are sanitized with `slugify()` / `short_slug()`, which is a good pattern and should be extended to `issue_id`: [src/maniple_mcp/worktree.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/worktree.py:34).
- Claude session recovery is more defensive than the Codex path. It parses JSONL entries, restricts matches to root user messages, and ignores malformed lines: [src/maniple_mcp/session_state.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/session_state.py:642).
- JSONL scanning is generally bounded by age and file-size heuristics, which reduces denial-of-service exposure from very large logs: [src/maniple_mcp/session_state.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/session_state.py:409), [src/maniple_mcp/idle_detection.py](/mnt/c/dev/projects/github/maniple/src/maniple_mcp/idle_detection.py:248).
- I did not find repository code that hardcodes tokens, credentials, or secret material.

## 5. Reviewer

OpenAI Codex
