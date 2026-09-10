from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME = re.compile(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{1,32}\Z")


def valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name))


@dataclass(frozen=True)
class AgentConfig:
    name: str
    backend: str
    # Optional additional focus; shared responsibilities are always in the team prompt.
    role: str = ""
    model: str | None = None
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not valid_name(self.name) or self.name == "system":
            raise ValueError(f"Invalid agent name: {self.name!r}")
        if self.backend not in {"codex", "claude", "mock", "command"}:
            raise ValueError(f"Unknown backend: {self.backend}")
        if not isinstance(self.role, str):
            raise ValueError("role must be a string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise ValueError("model must be a nonempty string")
        if not isinstance(self.command, (tuple, list)) or any(
            not isinstance(arg, str) or "\0" in arg for arg in self.command
        ):
            raise ValueError("command must be an argument array, not a shell command string")
        if self.backend == "command" and (not self.command or not self.command[0]):
            raise ValueError("The command backend requires a nonempty command array")


@dataclass(frozen=True)
class TeamConfig:
    agents: tuple[AgentConfig, ...]
    workspace: Path = field(default_factory=Path.cwd)
    workflow: str = "build"
    context_mode: str = "incremental"
    permission_mode: str = "phase_scoped"
    interaction_mode: str = "serial"
    turn_timeout: float = 0
    work_timeout: float = 0
    acceptance_timeout: float = 0
    idle_warning_seconds: float = 120
    turn_delay: float = 0.8
    proposal_version_limit: int = 5

    def __post_init__(self) -> None:
        if self.workflow not in {"build", "discussion"}:
            raise ValueError("workflow must be build or discussion")
        if self.context_mode not in {"incremental", "full"}:
            raise ValueError("context_mode must be incremental or full")
        if self.permission_mode not in {"phase_scoped", "full_auto"}:
            raise ValueError("permission_mode must be phase_scoped or full_auto")
        if self.interaction_mode not in {"serial", "chatroom"}:
            raise ValueError("interaction_mode must be serial or chatroom")
        if not self.agents or len({a.name for a in self.agents}) != len(self.agents):
            raise ValueError("At least one agent is required and names must be unique")
        if self.workflow == "build" and len(self.agents) < 2:
            raise ValueError("build requires at least two agents for independent peer judgment")
        for key in (
            "turn_timeout",
            "work_timeout",
            "acceptance_timeout",
            "idle_warning_seconds",
            "turn_delay",
        ):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be a finite, nonnegative number of seconds")
        limit = self.proposal_version_limit
        if type(limit) is not int or isinstance(limit, bool) or limit < 0:
            raise ValueError("proposal_version_limit must be a nonnegative whole number")
        if not self.workspace.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")


RENAMED_KEYS = {"check_timeout": "acceptance_timeout"}


def load_config(path: Path) -> TeamConfig:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown = set(data) - {"team", "agents"}
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    try:
        if not isinstance(data.get("team", {}), dict):
            raise ValueError("team must be a TOML table")
        if not isinstance(data.get("agents", []), list):
            raise ValueError("agents must use [[agents]] array tables")
        team = dict(data.get("team", {}))
        obsolete = set(team) & {"max_turns", "max_work_turns", "max_context_chars"}
        if obsolete:
            raise ValueError(
                "Remove obsolete limit settings: "
                + ", ".join(sorted(obsolete))
                + ". Conversation rounds, output, and shared context are now uncapped."
            )
        stale = {old: RENAMED_KEYS[old] for old in sorted(set(team) & set(RENAMED_KEYS))}
        if stale:
            raise ValueError(
                "Renamed in 0.2.0: "
                + ", ".join(f"{old} is now {new}" for old, new in stale.items())
            )
        if team.get("context_mode") == "session":
            raise ValueError(
                'Renamed in 0.2.0: context_mode = "session" is now context_mode = "incremental"'
            )
        workspace = Path(team.pop("workspace", ".")).expanduser()
        if not workspace.is_absolute():
            workspace = path.resolve().parent / workspace
        agents = tuple(AgentConfig(**agent) for agent in data.get("agents", []))
        return TeamConfig(agents=agents, workspace=workspace.resolve(), **team)
    except TypeError as exc:
        raise ValueError(f"Invalid configuration fields: {exc}") from exc


def demo_config() -> TeamConfig:
    return TeamConfig(
        agents=(
            AgentConfig("member_a", "mock"),
            AgentConfig("member_b", "mock"),
        ),
        turn_delay=0.15,
    )


DEFAULT_CONFIG = """# Install and sign in to codex and claude before starting.
[team]
workspace = "."
workflow = "build"
context_mode = "incremental"
# Independent resident agents think concurrently and publish without round-robin turns.
interaction_mode = "chatroom"
# Every phase: Codex full access; Claude auto approval with all built-in tools.
# Use phase_scoped to restore the previous restricted execution modes.
# Not every model applies auto approval; agent-team doctor checks the one you set.
permission_mode = "full_auto"
# There are no round, output-length, or shared-context limits.
# Hard timeouts are opt-in, in seconds; 0 means wait until completion or interruption.
turn_timeout = 0
work_timeout = 0
acceptance_timeout = 0
# Warn once per idle period without cancelling; 0 disables warnings.
idle_warning_seconds = 120
turn_delay = 0.8
# Pause for a human after this many proposals fail to reach consensus; 0 disables.
proposal_version_limit = 5

# All members share the same responsibilities. An optional role adds a focus, not a fixed job.
[[agents]]
name = "claude"
backend = "claude"
# model = "a-model-you-can-access"

[[agents]]
name = "codex"
backend = "codex"
# model = "a-model-you-can-access"
"""
