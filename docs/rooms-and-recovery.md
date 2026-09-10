# Rooms, context, and recovery

## Consensus documents

Each confirmed proposal is written before implementation to `docs/agent-team/<room-id>/consensus-vNNNN.md` inside the workspace. Documents record the approved scope, milestones, acceptance checks, and approving members. Use `/consensus` to inspect the latest consensus and previous versions.

Consensus remains revisable. Ordinary chat adds context; `/revise <guidance>` (alias `/redirect`) stops active work and reopens discussion. Members can also request revision themselves. Earlier documents, code, and contribution history remain available, but a revised proposal needs fresh unanimous approval. Generated documents are snapshots, not instructions to edit directly. If publication fails, resolve the filesystem problem and use `/resume`.

## Backend sessions

By default, each native member keeps its own resumable backend session: Codex uses a resident app-server; Claude uses a streaming CLI connection. The first turn receives the full public transcript, later turns receive the messages it has not seen plus current workflow instructions.

Failures, quota limits, and interruptions retain known session IDs and the last acknowledged cursor; recovery resumes that session with the missing messages and a reconciliation notice. A full rebuild is reserved for missing or incompatible sessions, or an explicit reset. The public event log remains authoritative.

## Room history

History lives in `<room>/events.sqlite3`. The default room path is relative to the directory where you launch agent-team, while `workspace` is relative to the configuration file. Deleting source files or `team.toml` does not clear the hidden `.agent-team` directory.

Reopening the same room restores history **paused**; use `/resume`. The one pause a room lifts by itself is a restart that is still waiting on a provider's quota reset: when that reset time arrives, the room runs its recovery checks and continues with nobody present, so a run can span a reset window overnight. To start fresh without deleting old history, choose an unused path: `agent-team --room .agent-team/new-room`. Changing team members or workspace also requires a new room. `/resume` on an already-running team does not trigger extra calls.

## Usage limits

**A room stuck in discussion with no error is usually a protocol failure, not a disagreement.** A member whose replies omit a well-formed `<team-action>` block never votes, so consensus cannot be reached; its peers may read the vote in the message text and believe it counted. Raise that member's `reasoning_effort` first — see [Configuration](configuration.md). The room now says when an action was written as ordinary text, and pauses for you if the same member does it twice.

**Either Claude or Codex reaching its usage limit pauses the entire team.** A reported reset time schedules a recovery check after a 30-second buffer; no fixed 5-hour delay remains. Unknown reset times require `/retry [member]` or `/resume`.

Discussion and work resume only after all limited members pass isolated checks. Codex reset metadata is read through its resident app-server; legacy serial Codex requires manual recovery when no timestamp is available. `/status` shows timing and remaining wait.

Manual pauses and other errors require explicit resume. A server restart does too, unless a quota reset falls due first. Old fixed-delay deadlines are ignored. Required votes and existing file changes are preserved.

Why the whole team pauses rather than continuing a member short: [ADR-0003](adr/0003-quota-pauses-the-whole-room.md).
