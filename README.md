# agent-team

Start with an idea. Claude Code, Codex, and other agents discuss it in a shared conversation, reach explicit consensus, then build and judge each other's work. Humans guide the team through a conversation-first CLI.

Requires Python 3.11+ on Linux, macOS, or WSL. Native Windows is not supported.

## Quick start

From this repository, try the account-free demo:

```bash
uv sync
uv run agent-team demo
```

The demo uses two mock agents to build a fixed greeting example, regardless of your idea. Its temporary files and history are removed on exit.

For real work, install and sign in to `claude` and `codex`, then install agent-team and open your project:

```bash
uv tool install --editable .
cd /path/to/project
agent-team
```

No preliminary `init` is needed. First launch creates `team.toml` if missing and opens the idea prompt. Existing configuration is reused, never silently replaced. Defaults select Claude and Codex, concurrent chatroom mode, full-auto permissions, the current project as workspace, and `.agent-team/default` for history. **Full-auto permits real file changes and command execution; use a trusted or disposable workspace.**

Send your goal, constraints, and acceptance criteria to begin. No model is called before you send an idea or resume existing work. After unanimous agreement, implementation starts without another human approval step. Calls use each CLI's local authentication, settings, and quota; an omitted `model` uses its CLI default.

Use `agent-team doctor` to check an existing configuration and executable availability; it does not verify authentication or quota. Startup options need no subcommand: `agent-team --config /path/to/team.toml --session /path/to/session`. Explicit configuration paths must exist. `agent-team init` remains optional when you want to configure the team before starting.

## How collaboration works

Idea → Discussion → Unanimous agreement → Shared implementation and peer judgment → Integration review → Acceptance checks

- **Equal responsibilities:** every member can propose, challenge, implement, and review. Names and backends do not assign permanent roles. Milestones are shared work, not isolated coding assignments; agents can revise each other's implementations.
- **Concurrent discussion, one writer:** members think independently and publish as they finish. Only one implementation or acceptance-check turn holds the workspace write lease. Formal readers finish before another writer starts; conversation-only turns may research but must not write, vote, or judge.
- **Explicit consensus and reciprocal review:** all members must approve the same proposal version. Each implementation checkpoint needs judgment from every other member; its author cannot self-approve. Rejections carry evidence and reopen work. The critic can take the next write turn.
- **Evidence before completion:** all members review the integrated result, then the coordinator runs the agreed acceptance commands. Failed checks reopen work. Completion means those checks passed, not that every possible defect is absent.

Public messages are ordered and retained in full. Busy agents receive accumulated messages at their next input boundary, not during an in-flight generation. Live drafts are separate from committed messages, and private reasoning is not shared. With nothing new to contribute, agents yield and listen; silence is not approval.

There are no application-imposed round, output-length, message-length, or shared-context limits. Hard timeouts are disabled by default. Provider limits, quota, and available memory/disk still apply. A notice after 120 seconds without observable output is informational: it does not cancel work or prove the agent is stuck.

## Agreements, context, and history

Each confirmed proposal is written before implementation to `docs/agent-team/<room-id>/consensus-vNNNN.md` inside the workspace. Documents record the approved scope, milestones, acceptance commands, and approving members. Use `/consensus` to inspect the latest agreement and previous versions.

Agreements remain revisable. Ordinary chat adds context; `/revise <guidance>` (alias `/redirect`) stops active work and reopens discussion. Agents can also request revision themselves. Earlier documents, code, and contribution history remain available, but a revised proposal needs fresh unanimous approval. Generated documents are snapshots, not instructions to edit directly. If publication fails, resolve the filesystem problem and use `/resume`.

By default, each native agent keeps its own resumable private session: Codex uses a resident app-server; Claude uses a streaming CLI connection. The first turn receives the full public transcript, later turns receive missing messages plus current workflow instructions. Private sessions are working memory; the public SQLite record is authoritative. Failed or interrupted sessions are rebuilt from public history when explicitly retried.

History lives in `<session>/events.sqlite3`. The default session path is relative to the directory where you launch agent-team, while `workspace` is relative to the configuration file. Deleting source files or `team.toml` does not clear the hidden `.agent-team` directory. Reopening the same session restores history **paused**; use `/resume`. To start fresh without deleting old history, choose an unused path: `agent-team --session .agent-team/new-session`. Changing team members or workspace also requires a new room.

## Using the CLI

Enter sends the complete draft; Alt+Enter or Ctrl-J inserts a newline. Multiline paste waits for explicit submission. Type `/` for command suggestions. The mouse wheel scrolls three display rows, including wrapped text, without moving input focus or changing your draft; PgUp/PgDn moves one screen. Reading holds a stable snapshot; scroll to the bottom or press Ctrl-End to follow live output again. Escape dismisses suggestions or detail views without interrupting agents or discarding your draft.

| Command | Effect |
| --- | --- |
| `/pause` / `/interrupt` | Finish active replies, then pause / cancel active turns and pause |
| `/resume` / `/retry [agent]` | Resume the team / retry one or all unavailable members |
| `/revise text` / `/redirect text` | Stop active work and reopen discussion with new guidance |
| `/next [agent]` | Run one eligible turn, then pause |
| `/plan`, `/tasks`, `/consensus` | Inspect work, votes, judgments, and approved documents |
| `/activity`, `/status`, `/sessions` | Inspect errors, team state, and private-session synchronization |
| `/reset-session [agent]` | While idle, forget private-session references and pause; retain public history and files |
| `/chat` / `/help` | Return to the conversation / show commands and shortcuts |
| `/quit` or Ctrl-D | Leave this terminal |

**Ctrl-C exits immediately when idle with no draft.** During active work, the first press requests interruption and another confirms exit. An unsent draft also requires confirmation; Escape or editing cancels it. If delivery stalls, another Ctrl-C forces local exit without guaranteeing delivery.

**Quota handling follows the backend:** confirmed Claude usage exhaustion cools down only that member; available peers continue, and the server retries Claude after 5 hours. Another quota failure starts another 5-hour wait. Codex usage exhaustion and other call failures pause the whole team and interrupt active turns; use `/resume` or `/retry` after resolving the issue. `/retry [agent]` can retry Claude early, but cannot bypass another blocking failure. Timers preserve their deadline across restarts and never resume a paused team. `/status` shows recovery details. Required votes are never waived; cancellation never rolls back existing file changes.

Leaving `agent-team` or `start` stops its server but retains history. For unattended work, use separate terminals in the same project directory:

```bash
agent-team serve                 # Terminal 1; requires an existing team.toml
agent-team join --name observer  # Terminal 2
```

Keep the foreground `serve` process alive. Leaving a `join` client with Ctrl-D or `/quit` does not stop or pause the team, even with no humans online; Ctrl-C still interrupts active work. From another directory, pass the same absolute `--session` path. Disconnected clients retain their visible history and draft until closed; leave and rejoin to reconnect. Uncertain requests are never automatically replayed.

## Configuration and safety

Edit [team.toml](team.toml) for settings. `workflow = "build"` requires at least two agents; `"discussion"` is chat-only and supports one. Add `[[agents]]` entries with unique names to use more agents or different models. Supported backends are `claude`, `codex`, `mock`, and `command`. An optional `role` adds a focus without changing shared responsibilities or permissions.

| Setting | Options / behavior |
| --- | --- |
| `interaction_mode` | `"chatroom"` for concurrent resident members; `"serial"` for single-floor turns |
| `context_mode` | `"session"` resumes private sessions; `"full"` uses fresh private conversations and full public context |
| `permission_mode` | `"full_auto"` or `"phase_scoped"`; see below |
| `turn_timeout`, `work_timeout`, `check_timeout` | Opt-in deadlines in seconds; `0` disables them |
| `idle_warning_seconds`, `turn_delay` | Inactivity notice interval and per-member delay; defaults are 120 and 0.8 seconds |

Generated configuration selects `chatroom` and `full_auto`; older configurations omitting those keys retain `serial` and `phase_scoped`. Full-auto applies throughout both chatroom and serial modes, including discussion, planning, judgment, review, and chat: Codex uses full access without sandbox restrictions; Claude uses native auto approval with all built-in tools, including web tools, not permission bypass. No phase-specific tool restrictions are applied. Native policies and host network restrictions still apply.

Only assigned implementation turns may modify project files. In full-auto this is a workflow instruction and scheduling rule, not a sandbox guarantee; use a container or VM when isolation is required. Use `phase_scoped` for phase-specific restrictions. Custom adapters and acceptance commands do not inherit a native CLI sandbox. Automatic commits, pushes, and deployments are outside the default workflow. The server listens on loopback and authenticates clients with a private connection token.

Custom commands receive a UTF-8 prompt on stdin and return JSONL deltas plus a completion event, with exit code 0. They must honor phase permissions themselves. See the [transport example](examples/command_agent.py) and [workflow protocol](docs/protocol.md) for integration details.

## Development and exports

```bash
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ruff format --check .
uv build
agent-team history --json        # Export all durable events; omit --json for Markdown messages
agent-team join --plain          # Line input / JSONL output; stdin EOF leaves the room
```

Optional [live smoke tests](scripts/) consume account quota and use bounded test deadlines, independent of runtime limits. Detailed references: [scheduling](docs/protocol.md#concurrent-room-scheduling), [consensus and revisions](docs/protocol.md#versioned-consensus-documents-and-revision), [recovery](docs/protocol.md#pausing-recovery-and-floor-control), and [private sessions](docs/protocol.md#private-session-synchronization).
