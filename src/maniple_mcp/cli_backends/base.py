"""
Base protocol for CLI agent backends.

Defines the interface that all CLI backends (Claude, Codex, etc.) must implement.
This abstraction allows claude-team to orchestrate different agent CLIs.
"""

import re
import shlex
from abc import abstractmethod
from typing import Literal, Protocol, runtime_checkable


# A valid POSIX shell environment variable name. Used to reject env keys that
# could inject shell syntax (keys cannot be quoted without breaking KEY=value).
_VALID_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@runtime_checkable
class AgentCLI(Protocol):
    """
    Protocol defining the interface for agent CLI backends.

    Each implementation encapsulates the CLI-specific details:
    - Command and arguments
    - Ready detection patterns
    - Idle/completion detection method
    - Settings/hook injection support
    """

    @property
    @abstractmethod
    def engine_id(self) -> str:
        """
        Unique identifier for this CLI engine (e.g., "claude", "codex").

        Used for configuration, logging, and distinguishing between backends.
        """
        ...

    @abstractmethod
    def command(self) -> str:
        """
        Return the CLI executable name or path.

        Examples: "claude", "codex", "/usr/local/bin/custom-agent"
        """
        ...

    @abstractmethod
    def build_args(
        self,
        *,
        dangerously_skip_permissions: bool = False,
        settings_file: str | None = None,
        plugin_dir: str | list[str] | None = None,
    ) -> list[str]:
        """
        Build the argument list for the CLI command.

        Args:
            dangerously_skip_permissions: If True, add flag to skip permission prompts
            settings_file: Optional path to settings file for hook injection
            plugin_dir: Optional path(s) to plugin directory (single string or list)

        Returns:
            List of command-line arguments (not including the command itself)
        """
        ...

    @abstractmethod
    def ready_patterns(self) -> list[str]:
        """
        Return patterns that indicate the CLI is ready for input.

        These patterns are searched for in terminal output to detect when
        the agent has started and is ready to receive prompts.

        Returns:
            List of strings to search for in terminal output
        """
        ...

    @abstractmethod
    def idle_detection_method(self) -> Literal["stop_hook", "jsonl_stream", "none"]:
        """
        Return the method used to detect when the agent finishes responding.

        - "stop_hook": Uses a Stop hook that fires when the agent completes
        - "jsonl_stream": Monitors JSONL output for completion markers
        - "none": No idle detection available (must use timeouts)

        Returns:
            The detection method identifier
        """
        ...

    @abstractmethod
    def supports_settings_file(self) -> bool:
        """
        Return whether this CLI supports --settings flag for hook injection.

        If False, build_args() should ignore the settings_file parameter.
        """
        ...

    def build_full_command(
        self,
        *,
        dangerously_skip_permissions: bool = False,
        settings_file: str | None = None,
        plugin_dir: str | list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> str:
        """
        Build the complete command string including env vars.

        This is a convenience method that combines command(), build_args(),
        and optional environment variables into a single shell command string.

        Args:
            dangerously_skip_permissions: Skip permission prompts
            settings_file: Settings file for hook injection
            plugin_dir: Optional path(s) to plugin directory (single string or list)
            env_vars: Environment variables to prepend

        Returns:
            Complete command string ready for shell execution

        Security:
            Every interpolated value is neutralized against shell injection:

            - The command override (``self.command()``) is re-tokenized with
              ``shlex.split()`` and each token re-quoted. This preserves
              legitimate multi-token overrides (e.g. ``"my-wrapper run"`` or a
              quoted spaced path) while a malicious override such as
              ``"claude; rm -rf ~"`` collapses into a harmless single command
              name rather than injecting a second command.
            - Each argument and each env value is escaped with ``shlex.quote()``;
              benign flags pass through unchanged.
            - Env var *names* are validated against ``_VALID_ENV_KEY`` (they
              cannot be safely quoted in ``KEY=value`` form), rejecting any key
              that could carry shell syntax.

        Raises:
            ValueError: If an environment variable name is not a valid shell
                identifier.
        """
        cmd_parts = [shlex.quote(tok) for tok in shlex.split(self.command())]
        args = self.build_args(
            dangerously_skip_permissions=dangerously_skip_permissions,
            settings_file=settings_file if self.supports_settings_file() else None,
            plugin_dir=plugin_dir,
        )
        cmd_parts.extend(shlex.quote(arg) for arg in args)
        cmd = " ".join(cmd_parts)

        if env_vars:
            for key in env_vars:
                if not _VALID_ENV_KEY.match(key):
                    raise ValueError(
                        f"Invalid environment variable name: {key!r}"
                    )
            env_exports = " ".join(
                f"{k}={shlex.quote(v)}" for k, v in env_vars.items()
            )
            cmd = f"{env_exports} {cmd}"

        return cmd
