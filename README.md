# agent-team

Start with an idea. Claude Code, Codex, and other agents discuss it in a shared conversation, reach explicit consensus, then build and judge each other's work. Humans guide the team through a conversation-first CLI.

Requires Python 3.11+ on Linux, macOS, or WSL. Native Windows is not supported.

## Quick start

Try the account-free demo from this repository:

```bash
uv sync
uv run agent-team demo
```

The demo uses two mock members to build a fixed greeting example, regardless of your idea. Its temporary files and history are removed on exit.

For real work, install and sign in to `claude` and `codex`, then install agent-team and open your project:

```bash
uv tool install --editable .
cd /path/to/project
agent-team
```

No preliminary `init` is needed. First launch creates `team.toml` if missing and opens the idea prompt. Existing configuration is reused, never silently replaced. Defaults select Claude and Codex, concurrent chatroom mode, full-auto permissions, the current project as workspace, and `.agent-team/default` for history.

> **Full-auto permits real file changes and command execution; use a trusted or disposable workspace.**

Send your goal, constraints, and acceptance criteria to begin. No model is called before you send an idea or resume existing work. After consensus, implementation starts without another human approval step. Calls use each CLI's local authentication, settings, and quota; an omitted `model` uses its CLI default.

Use `agent-team doctor` to check an existing configuration and executable availability; it does not verify authentication or quota. Startup options need no subcommand: `agent-team --config /path/to/team.toml --room /path/to/room`. Explicit configuration paths must exist. `agent-team init` remains optional when you want to configure the team before starting.

## How collaboration works

Idea → Discussion → Consensus → Shared implementation and peer judgment → Integration review → Acceptance checks

- **Equal responsibilities.** Every member can propose, challenge, implement, and review. Names and backends do not assign permanent roles. Milestones are shared work, not isolated coding assignments; members can revise each other's implementations.
- **Concurrent discussion, one writer.** Members think independently and publish as they finish. Only one implementation or acceptance turn holds the workspace write lease. Formal readers finish before another writer starts; conversation-only turns may research but must not write, vote, or judge.
- **Explicit consensus and reciprocal review.** All members must approve the same proposal version. Each implementation checkpoint needs judgment from every other member; its author cannot self-approve. Rejections carry evidence and reopen work. The critic can take the next write turn.
- **Evidence before completion.** All members review the integrated result, then the coordinator runs the agreed acceptance checks. Failed checks reopen work. Completion means those checks passed, not that every possible defect is absent.

Public messages are ordered and retained in full. Busy members receive accumulated messages at their next input boundary, not during an in-flight generation. Live drafts are separate from committed messages, and private reasoning is not shared. With nothing new to contribute, members yield and listen; silence is not approval.

There are no application-imposed round, output-length, message-length, or shared-context limits, and hard timeouts are disabled by default ([ADR-0004](docs/adr/0004-no-application-imposed-limits.md)). Provider limits, quota, and available memory/disk still apply. A notice after 120 seconds without observable output is informational: it does not cancel work or prove the member is stuck.

## Reference

| Document | Covers |
| --- | --- |
| [CLI reference](docs/cli.md) | Keys, commands, leaving, running unattended |
| [Configuration and safety](docs/configuration.md) | `team.toml` settings, permission modes, custom backends, 0.2.0 renames |
| [Rooms, context, and recovery](docs/rooms-and-recovery.md) | Consensus documents, backend sessions, room history, usage limits |
| [Workflow protocol](docs/protocol.md) | The wire protocol, for backend authors |
| [CONTEXT.md](CONTEXT.md) | The project's vocabulary |
| [Decisions](docs/adr/) | Why the design is the way it is |

## Development

```bash
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ruff format --check .
uv build
agent-team history --json        # Export all durable events; omit --json for Markdown messages
agent-team join --plain          # Line input / JSONL output; stdin EOF leaves the room
```

Optional [live smoke tests](scripts/) consume account quota and use bounded test deadlines, independent of runtime limits.
