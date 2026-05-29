"""
Tests for CLI backends module.

Tests the AgentCLI protocol and its implementations (Claude, Codex).
"""

import json
import os
from unittest.mock import patch

import pytest

from maniple_mcp import config as config_module
from maniple_mcp.cli_backends import (
    AgentCLI,
    ClaudeCLI,
    CodexCLI,
    claude_cli,
    codex_cli,
    get_cli_backend,
)


@pytest.fixture(autouse=True)
def config_path(tmp_path, monkeypatch):
    """Point config path to a temp location for deterministic CLI tests."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


class TestAgentCLIProtocol:
    """Tests for the AgentCLI protocol."""

    def test_claude_cli_is_agent_cli(self):
        """ClaudeCLI should be an instance of AgentCLI protocol."""
        assert isinstance(claude_cli, AgentCLI)

    def test_codex_cli_is_agent_cli(self):
        """CodexCLI should be an instance of AgentCLI protocol."""
        assert isinstance(codex_cli, AgentCLI)


class TestClaudeCLI:
    """Tests for Claude CLI backend."""

    def test_engine_id(self):
        """Engine ID should be 'claude'."""
        cli = ClaudeCLI()
        assert cli.engine_id == "claude"

    def test_command_default(self):
        """Default command should be 'claude'."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            assert cli.command() == "claude"

    def test_command_from_env(self):
        """Command should respect MANIPLE_COMMAND env var."""
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "happy"}):
            cli = ClaudeCLI()
            assert cli.command() == "happy"

    def test_command_from_deprecated_env(self):
        """Command should fall back to deprecated CLAUDE_TEAM_COMMAND env var."""
        with patch.dict(os.environ, {"CLAUDE_TEAM_COMMAND": "happy"}):
            cli = ClaudeCLI()
            assert cli.command() == "happy"

    def test_command_env_precedence(self):
        """MANIPLE_COMMAND should take precedence over CLAUDE_TEAM_COMMAND."""
        with patch.dict(
            os.environ,
            {"MANIPLE_COMMAND": "new", "CLAUDE_TEAM_COMMAND": "old"},
        ):
            cli = ClaudeCLI()
            assert cli.command() == "new"

    def test_command_from_config(self, config_path):
        """Command should use config when env var is unset."""
        config_path.write_text(json.dumps({
            "version": 1,
            "commands": {"claude": "/from/config"},
        }))
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            assert cli.command() == "/from/config"

    def test_command_env_overrides_config(self, config_path):
        """Env var should override config command."""
        config_path.write_text(json.dumps({
            "version": 1,
            "commands": {"claude": "/from/config"},
        }))
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "from-env"}):
            cli = ClaudeCLI()
            assert cli.command() == "from-env"

    def test_build_args_empty_default(self):
        """Default args should be empty list."""
        cli = ClaudeCLI()
        args = cli.build_args()
        assert args == []

    def test_build_args_skip_permissions(self):
        """Should add --dangerously-skip-permissions flag."""
        cli = ClaudeCLI()
        args = cli.build_args(dangerously_skip_permissions=True)
        assert "--dangerously-skip-permissions" in args

    def test_build_args_settings_file_default_command(self):
        """Should add --settings flag for default claude command."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            args = cli.build_args(settings_file="/path/to/settings.json")
            assert "--settings" in args
            assert "/path/to/settings.json" in args

    def test_build_args_settings_file_skipped_for_custom_command(self):
        """Should NOT add --settings flag for custom commands like 'happy'."""
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "happy"}):
            cli = ClaudeCLI()
            args = cli.build_args(settings_file="/path/to/settings.json")
            assert "--settings" not in args

    def test_ready_patterns_not_empty(self):
        """Ready patterns should not be empty."""
        cli = ClaudeCLI()
        patterns = cli.ready_patterns()
        assert len(patterns) > 0

    def test_ready_patterns_includes_prompt(self):
        """Ready patterns should include the '>' prompt."""
        cli = ClaudeCLI()
        patterns = cli.ready_patterns()
        assert ">" in patterns

    def test_idle_detection_method(self):
        """Idle detection should use stop_hook."""
        cli = ClaudeCLI()
        assert cli.idle_detection_method() == "stop_hook"

    def test_supports_settings_file_default_command(self):
        """Should support settings file for default claude command."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            assert cli.supports_settings_file() is True

    def test_supports_settings_file_custom_command(self):
        """Should NOT support settings file for custom commands."""
        with patch.dict(os.environ, {"MANIPLE_COMMAND": "happy"}):
            cli = ClaudeCLI()
            assert cli.supports_settings_file() is False

    def test_build_full_command_simple(self):
        """build_full_command should combine command and args."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(dangerously_skip_permissions=True)
            assert cmd == "claude --dangerously-skip-permissions"

    def test_build_full_command_with_env_vars(self):
        """build_full_command should prepend env vars."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(env_vars={"FOO": "bar", "BAZ": "qux"})
            assert "FOO=bar" in cmd
            assert "BAZ=qux" in cmd
            assert cmd.endswith("claude")


class TestBuildFullCommandInjectionSafety:
    """
    Security: build_full_command is the shared command builder used by BOTH the
    iTerm and tmux backends. It must shell-escape every interpolated value so an
    attacker-influenced input cannot inject shell syntax (MAN-SEC-001).
    """

    def _claude(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            return ClaudeCLI()

    def test_malicious_env_value_is_quoted(self):
        """An env value with shell metacharacters must survive as one literal token."""
        import shlex

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(env_vars={"EVIL": "x; rm -rf /"})
            # A POSIX shell parser sees the assignment as a single token — the
            # ';' is literal data, not a command separator.
            tokens = shlex.split(cmd)
            assert "EVIL=x; rm -rf /" in tokens
            assert "claude" in tokens

    def test_malicious_plugin_dir_is_quoted(self):
        """A plugin_dir with command substitution must be a single literal token."""
        import shlex

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(plugin_dir="/tmp/$(touch pwned)")
            tokens = shlex.split(cmd)
            # The substitution survives verbatim as one argument → not evaluated.
            assert tokens == ["claude", "--plugin-dir", "/tmp/$(touch pwned)"]

    def test_benign_flags_pass_through_unchanged(self):
        """Flags without metacharacters should not gain spurious quoting."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            cmd = cli.build_full_command(dangerously_skip_permissions=True)
            assert cmd == "claude --dangerously-skip-permissions"

    def test_malicious_command_override_is_neutralized(self):
        """A command override carrying ';' must not inject a second command."""
        import shlex

        with patch.dict(os.environ, {"MANIPLE_COMMAND": "claude; touch /tmp/pwned #"}):
            cli = ClaudeCLI()
            cmd = cli.build_full_command()
            # Parsed by a POSIX shell, the override tokenizes to literal command
            # name + args — the ';' is data inside a token, never a separator.
            tokens = shlex.split(cmd)
            assert tokens[0] == "claude;"  # harmless: "command not found"
            assert "touch" in tokens  # present, but as a literal arg, not a command
            # The rendered string must quote the ';' so a shell cannot split on it.
            assert "'claude;'" in cmd

    def test_multi_token_command_override_preserved(self):
        """A legitimate multi-token override (wrapper + subcommand) still works."""
        import shlex

        with patch.dict(os.environ, {"MANIPLE_COMMAND": "my-wrapper run"}):
            cli = ClaudeCLI()
            cmd = cli.build_full_command(dangerously_skip_permissions=True)
            assert shlex.split(cmd) == [
                "my-wrapper",
                "run",
                "--dangerously-skip-permissions",
            ]

    def test_invalid_env_key_rejected(self):
        """An env var name with shell metacharacters must be rejected."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_COMMAND", None)
            cli = ClaudeCLI()
            with pytest.raises(ValueError):
                cli.build_full_command(env_vars={"BAD; touch x #": "1"})


class TestCodexCLI:
    """Tests for Codex CLI backend."""

    def test_engine_id(self):
        """Engine ID should be 'codex'."""
        cli = CodexCLI()
        assert cli.engine_id == "codex"

    def test_command_default(self):
        """Default command should be 'codex'."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_CODEX_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_CODEX_COMMAND", None)
            cli = CodexCLI()
            assert cli.command() == "codex"

    def test_command_from_env(self):
        """Command should respect MANIPLE_CODEX_COMMAND env var."""
        with patch.dict(os.environ, {"MANIPLE_CODEX_COMMAND": "happy codex"}):
            cli = CodexCLI()
            assert cli.command() == "happy codex"

    def test_command_from_deprecated_env(self):
        """Command should fall back to deprecated CLAUDE_TEAM_CODEX_COMMAND env var."""
        with patch.dict(os.environ, {"CLAUDE_TEAM_CODEX_COMMAND": "happy codex"}):
            cli = CodexCLI()
            assert cli.command() == "happy codex"

    def test_command_env_precedence(self):
        """MANIPLE_CODEX_COMMAND should take precedence over CLAUDE_TEAM_CODEX_COMMAND."""
        with patch.dict(
            os.environ,
            {"MANIPLE_CODEX_COMMAND": "new", "CLAUDE_TEAM_CODEX_COMMAND": "old"},
        ):
            cli = CodexCLI()
            assert cli.command() == "new"

    def test_command_from_config(self, config_path):
        """Command should use config when env var is unset."""
        config_path.write_text(json.dumps({
            "version": 1,
            "commands": {"codex": "/from/config"},
        }))
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_CODEX_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_CODEX_COMMAND", None)
            cli = CodexCLI()
            assert cli.command() == "/from/config"

    def test_command_env_overrides_config(self, config_path):
        """Env var should override config command."""
        config_path.write_text(json.dumps({
            "version": 1,
            "commands": {"codex": "/from/config"},
        }))
        with patch.dict(os.environ, {"MANIPLE_CODEX_COMMAND": "from-env"}):
            cli = CodexCLI()
            assert cli.command() == "from-env"

    def test_build_args_empty_default(self):
        """Default args should be empty list."""
        cli = CodexCLI()
        args = cli.build_args()
        assert args == []

    def test_build_args_skip_permissions_maps_to_bypass_approvals(self):
        """skip_permissions should map to --dangerously-bypass-approvals-and-sandbox for Codex."""
        cli = CodexCLI()
        args = cli.build_args(dangerously_skip_permissions=True)
        assert "--dangerously-bypass-approvals-and-sandbox" in args

    def test_build_args_settings_file_ignored(self):
        """Settings file should be ignored (Codex doesn't support it)."""
        cli = CodexCLI()
        args = cli.build_args(settings_file="/path/to/settings.json")
        assert "--settings" not in args
        assert "/path/to/settings.json" not in args

    def test_ready_patterns_not_empty(self):
        """Ready patterns should not be empty."""
        cli = CodexCLI()
        patterns = cli.ready_patterns()
        assert len(patterns) > 0

    def test_ready_patterns_include_codex_0124_tui(self):
        """Ready detection should recognize Codex v0.124's boxed TUI."""
        cli = CodexCLI()
        patterns = cli.ready_patterns()
        assert "›" in patterns
        assert ">_ OpenAI Codex" in patterns
        assert "OpenAI Codex (v" in patterns

    def test_idle_detection_method(self):
        """Idle detection should use JSONL streaming (captures output via tee)."""
        cli = CodexCLI()
        assert cli.idle_detection_method() == "jsonl_stream"

    def test_supports_settings_file(self):
        """Codex should NOT support settings file."""
        cli = CodexCLI()
        assert cli.supports_settings_file() is False

    def test_build_full_command_simple(self):
        """build_full_command should return just 'codex' for defaults."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_CODEX_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_CODEX_COMMAND", None)
            cli = CodexCLI()
            cmd = cli.build_full_command()
            assert cmd == "codex"

    def test_build_full_command_with_bypass_approvals(self):
        """build_full_command should add --dangerously-bypass-approvals-and-sandbox."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MANIPLE_CODEX_COMMAND", None)
            os.environ.pop("CLAUDE_TEAM_CODEX_COMMAND", None)
            cli = CodexCLI()
            cmd = cli.build_full_command(dangerously_skip_permissions=True)
            assert cmd == "codex --dangerously-bypass-approvals-and-sandbox"

    def test_build_full_command_with_env_var(self):
        """build_full_command should use MANIPLE_CODEX_COMMAND."""
        with patch.dict(os.environ, {"MANIPLE_CODEX_COMMAND": "happy codex"}):
            cli = CodexCLI()
            cmd = cli.build_full_command(dangerously_skip_permissions=True)
            assert cmd == "happy codex --dangerously-bypass-approvals-and-sandbox"


class TestGetCliBackend:
    """Tests for the get_cli_backend factory function."""

    def test_get_claude_backend(self):
        """Should return ClaudeCLI for 'claude'."""
        cli = get_cli_backend("claude")
        assert isinstance(cli, ClaudeCLI)

    def test_get_codex_backend(self):
        """Should return CodexCLI for 'codex'."""
        cli = get_cli_backend("codex")
        assert isinstance(cli, CodexCLI)

    def test_get_unknown_backend_raises(self):
        """Should raise ValueError for unknown agent type."""
        with pytest.raises(ValueError) as exc_info:
            get_cli_backend("unknown_agent")
        assert "Unknown agent type" in str(exc_info.value)
        assert "unknown_agent" in str(exc_info.value)

    def test_get_backend_default_is_claude(self):
        """Default agent type should be 'claude'."""
        cli = get_cli_backend()
        assert isinstance(cli, ClaudeCLI)
