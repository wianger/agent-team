# Configuration and safety

Edit [team.toml](../team.toml) for settings. `workflow = "build"` requires at least two agents; `"discussion"` is chat-only and supports one. Add `[[agents]]` entries with unique names to use more agents or different models. Supported backends are `claude`, `codex`, `mock`, and `command`. An optional `role` adds a focus without changing shared responsibilities or permissions.

## Settings

| Setting | Options / behavior |
| --- | --- |
| `interaction_mode` | `"chatroom"` for concurrent resident members; `"serial"` for single-floor turns |
| `context_mode` | `"incremental"` sends each member only the messages it has not seen; `"full"` uses fresh backend conversations and full public context |
| `permission_mode` | `"full_auto"` or `"phase_scoped"`; see below |
| `turn_timeout`, `work_timeout`, `acceptance_timeout` | Opt-in deadlines in seconds; `0` disables them |
| `idle_warning_seconds`, `turn_delay` | Inactivity notice interval and per-member delay; defaults are 120 and 0.8 seconds |
| `proposal_version_limit` | Pause for a human after this many proposals fail to reach consensus; default 5, `0` disables |

Generated configuration selects `chatroom` and `full_auto`; older configurations omitting those keys retain `serial` and `phase_scoped`.

## Permissions

Full-auto depends on the model applying Claude's auto approval mode. Some models accept `--permission-mode auto` and run as `default` instead, which denies every write and fails each turn before it starts; `agent-team doctor` reports this for the model you configured. Use `phase_scoped` with those models.

Full-auto applies throughout both chatroom and serial modes, including discussion, planning, judgment, review, and chat: Codex uses full access without sandbox restrictions; Claude uses native auto approval with all built-in tools, including web tools, not permission bypass. No phase-specific tool restrictions are applied. Native policies and host network restrictions still apply.

Only the member holding the write lease may modify project files. In full-auto this is a workflow instruction and scheduling rule, not a sandbox guarantee; use a container or VM when isolation is required. Use `phase_scoped` for phase-specific restrictions. Custom adapters and acceptance checks do not inherit a native CLI sandbox. Automatic commits, pushes, and deployments are outside the default workflow. The server listens on loopback and authenticates clients with a private connection token.

## Custom backends

Custom commands receive a UTF-8 prompt on stdin and return JSONL deltas plus a completion event, with exit code 0. They must honor phase permissions themselves. See the [transport example](../examples/command_agent.py) and [workflow protocol](protocol.md) for integration details.

## Renamed in 0.2.0

Old names are refused with an error naming the replacement.

| Before 0.2.0 | Now |
| --- | --- |
| `--session PATH` | `--room PATH` |
| `context_mode = "session"` | `context_mode = "incremental"` |
| `check_timeout` | `acceptance_timeout` |
| `/tasks` | `/milestones` |

The propose action's `tasks`, `acceptance` and `checks` keys became `milestones`, `acceptance_criteria` and `acceptance_checks`. Rooms recorded before 0.2.0 are refused with an explanation; read them with `agent-team history` and start new work in a new room.
