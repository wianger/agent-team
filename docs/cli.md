# CLI reference

## Keys and input

Enter sends the complete draft; Alt+Enter or Ctrl-J inserts a newline. Multiline paste waits for explicit submission. Type `/` for command suggestions.

The mouse wheel scrolls three display rows, including wrapped text, without moving input focus or changing your draft; PgUp/PgDn moves one screen. Reading holds a stable snapshot; scroll to the bottom or press Ctrl-End to follow live output again. Escape dismisses suggestions or detail views without interrupting members or discarding your draft.

## Commands

| Command | Effect |
| --- | --- |
| `/pause` / `/interrupt` | Finish active replies, then pause / cancel active turns and pause |
| `/resume` / `/retry [member]` | Resume the team / retry one or all unavailable members |
| `/revise text` / `/redirect text` | Stop active work and reopen discussion with new guidance |
| `/next [member]` | Run one eligible turn, then pause |
| `/plan`, `/milestones`, `/consensus` | Inspect work, votes, judgments, and approved documents |
| `/activity`, `/status`, `/sessions` | Inspect errors, team state, and backend-session synchronization |
| `/reset-session [member]` | While idle, forget backend-session references and pause; retain public history and files |
| `/chat` / `/help` | Return to the conversation / show commands and shortcuts |
| `/quit` or Ctrl-D | Leave this terminal |

## Leaving

**Ctrl-C exits immediately when idle with no draft.** During active work, the first press requests interruption and another confirms exit. An unsent draft also requires confirmation; Escape or editing cancels it. If delivery stalls, another Ctrl-C forces local exit without guaranteeing delivery.

## Running unattended

Leaving `agent-team` or `start` stops its server but retains history. For unattended work, use separate terminals in the same project directory:

```bash
agent-team serve                 # Terminal 1; requires an existing team.toml
agent-team join --name observer  # Terminal 2
```

Keep the foreground `serve` process alive. Leaving a `join` client with Ctrl-D or `/quit` does not stop or pause the team, even with no humans online; Ctrl-C still interrupts active work. From another directory, pass the same absolute `--room` path. Disconnected clients retain their visible history and draft until closed; leave and rejoin to reconnect. Uncertain requests are never automatically replayed.
