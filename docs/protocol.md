# Workflow action protocol

Humans use messages and CLI commands. Backend authors use this protocol. Built-in adapters include phase instructions and current workflow state in every prompt. Persistent CLI sessions receive the full public transcript on creation and missing public messages on subsequent turns. Stateless/full mode always receives the complete transcript.

Every member receives the same shared responsibilities for independent analysis, discussion, implementation, and reciprocal review. There are no backend-specific default specialties or permanent writer/reviewer roles. The optional `role` configuration field adds a milestone-specific focus alongside these responsibilities; it never changes phase permissions, floor eligibility, or milestone ownership. An empty or omitted `role` adds no focus. Current phase and contribution authorship determine who can implement or judge.

Execution permissions are separate from workflow authorization. The shipped configuration selects `interaction_mode = "chatroom"` and `permission_mode = "full_auto"`. Full-auto applies to every phase and lane, including discussion, planning, implementation, judgment, review, and chat. Codex receives `dangerFullAccess` with `approvalPolicy: "never"` on every turn, including resumed sessions. Claude uses `auto` with `--permission-prompts none` and `--tools default`, including web tools. Its registered `PreToolUse` callback does not restrict tools by phase in full-auto; it returns an empty result during an active turn to preserve native auto checks, and denies tool calls after the turn ends. Claude must confirm and retain `permissionMode: "auto"`; other modes fail closed. Native policies and host network restrictions still apply.

Legacy `serial` mode retains per-invocation CLIs with the same full-auto permissions. In full-auto, non-writing phases are workflow instructions, not sandbox or tool restrictions; all members may use tools for research but only the assigned implementation turn may modify project files. `phase_scoped` remains the fallback when the permission key is omitted: Codex uses read-only for nonwriters and workspace-write for writers; Claude uses dontAsk with Read/Glob/Grep for formal readers, acceptEdits with write/Bash tools for writers, and no tools for discussion/chat. The resident hook enforces those phase-scoped tool restrictions. Configuration fingerprints include interaction and permission modes and the context protocol version; changing them rebuilds private context from public history. These controls coordinate cooperative members, not malicious full-access processes or external writers.

End formal work replies with one `<team-action>JSON</team-action>` block, after the public explanation. Plain discussion may omit it. When several blocks appear the last one is the action, and text after it is discarded rather than failing the turn — a closing remark is ordinary output and cannot change which action was meant. An unterminated block or a payload that is not a JSON object with an action string still fails. A rejected action records nothing and does not end the run: the coordinator tells its author what was wrong and the member sends a corrected action. A second rejection in a row from the same member pauses the team for a human. Conversation-only turns may attach only a `request_revision` block, never an approval, checkpoint, or verdict. Interim commentary must not include action blocks. No text may follow the final block. Actions take effect only after a successfully completed invocation; transport `done` is not project completion.

There are no round, text-length, milestone-count, or command-count budgets. Required types, nonempty fields, valid identifiers, real file paths, dependency order, and matching versions remain enforced.

## Concurrent room scheduling

In chatroom mode every configured member has a permanent worker, an independent private connection, and an event-driven inbox. Connections are started on first work and reused after successful turns. Each private conversation has at most one in-flight generation; different members can generate concurrently. Messages become public atomically on completion, without a global speaking lock. Human clients receive transient drafts immediately, keyed by `turn_id`; private reasoning and raw tool output are excluded.

`turn.started` and `turn.finished` replace serial `floor.granted`/`floor.released` events. They include `speaker`, `turn_id`, workflow `phase`, `lane` (`work` or `chat`), and `context_through`. `turn.context` records the input prompt hash. `state.active_turns` lists all active invocations; `state.active` remains a representative entry for older clients. `state.writer` identifies the exclusive implementation/check runner. `state.runtimes` exposes per-member state, errors, process IDs, and pending-message counts.

Both schedulers emit transient `turn.activity` events (`speaker`, `turn_id`) for observed backend or check I/O, including private tool activity. Reports are limited to one per second per invocation, except that activity immediately following an idle warning is always reported. They contain no tool arguments, output, or reasoning; they are not stored, added to context, or used to wake agents. The terminal shows elapsed observation time and time since the latest activity; silence is not proof of a stall. Joining mid-turn starts a new observation window rather than claiming to know the original start time. Successful quota recovery clears the matching obsolete error banner, not unrelated errors or the Activity history, and never overrides the coordinator's pause state.

New public messages are retained for every worker. This implementation synchronizes busy members at their next input boundary, not by injecting into an in-flight generation. Own publications do not wake their author unless another public message or workflow change requires attention. `[[PASS]]` is neither a message nor a vote; quiescent workers report `waiting_messages` and wait without model polling.

Formal work uses a generation fence: room revision, proposal version, phase, decision epoch, and checkpoint identity/revision. New proposals, objections, checkpoints, and requested repairs invalidate older formal decisions. Late concurrent actions are stored as `rejected_action` plus `rejection`; they never alter the workflow, and the member can synchronize and reconsider. Duplicate approvals do not generate feedback loops. Unanimous discussion remains in `discussion` until outstanding formal discussion completes, then a durable system message records the transition to implementation.

Discussion and eligible formal reviews can run concurrently. Writers/checks wait for all formal readers, including stale readers, to finish. Only one writer/check runner holds the lease. Other members may use the conversation-only `chat` lane while work proceeds; its output cannot approve or judge, and members are instructed not to modify files. Full-auto chat may use tools for research and may request a new discussion of the consensus as described below. Informal observations of changing files are not stable-snapshot review evidence. Custom backends must honor the workflow scope themselves; native permissions follow the configured mode described above. Do not leave background writers running beyond a checkpoint.

## Proposal and consensus

```json
{
  "action": "propose",
  "summary": "Goal, scope, approach, assumptions, and tradeoffs",
  "acceptance_criteria": ["Observable outcome"],
  "milestones": [
    {"id":"T1","title":"Shared module","details":"Implement and judge the interface","depends_on":[]},
    {"id":"T2","title":"Shared tests","details":"Cover the acceptance criteria","depends_on":["T1"]}
  ],
  "acceptance_checks": [["python3","-m","unittest","discover","-s","tests"]]
}
```

Milestones need unique IDs and complete, acyclic dependencies. Plans require at least one milestone, acceptance criterion, and meaningful executable command. Commands must be nonempty argument arrays, not shell strings. The optional legacy `owner` must name a configured member, but neither restricts who may implement nor controls floor scheduling.

A proposal creates a new version, clears previous votes, and counts as its author's approval. The proposer must also explicitly approve on a later turn:

```json
{"action":"approve","version":1}
{"action":"object","version":1,"reason":"Specific unresolved issue"}
```

Proposing approves the proposal it creates; every other member still approves explicitly. An objection revokes existing approvals. Only unanimous approval of the same version, without outstanding objections, starts implementation.

A member may replace another member's standing proposal only after objecting to it, so displacing someone else's plan costs a recorded reason; an author may always refine its own. Once `proposal_version_limit` proposals (default 5) have failed to reach consensus, the room pauses for a human instead of proposing again. Plain discussion may omit an action; `[[PASS]]` yields the turn without voting.

## Versioned consensus documents and revision

Build workflow snapshots additionally carry `document_namespace`, `consensus_history`, `revision_base`, and `revision_request`. Each immutable consensus record contains the exact proposal, all approving members, proposal version, recording time, workspace-relative document path, preceding version (`supersedes`), and any revision request. A confirmed record is stored in the same durable workflow snapshot as the consensus decision; file generation is a replayable projection of that committed intent.

The coordinator generates `docs/agent-team/<document_namespace>/consensus-vNNNN.md`. Different room namespaces avoid collisions, and proposal versions need not be consecutive. In serial mode, the final approval commits the record; in chatroom mode, a system message commits confirmation only after all in-flight formal discussion drains without an objection. No provisional vote creates a document. `/pause` permits active discussion to finish and be documented without authorizing an implementation turn.

The coordinator publishes a complete file using create-only atomic linking and records `consensus.saved`. `state.consensus_documents` reports `ready_versions` and any export `error`. No further implementation starts while approved records remain unmaterialized. Errors pause with `reason: "document_error"`; `/resume` retries file publication, not the already-committed model invocation. Identical existing content is accepted without rewriting; differing content, special files, and symlinked document paths are rejected without overwrite. Native full-access writers and external editors are still expected to respect generated records; this is not an OS sandbox.

On restart, missing approved files are rebuilt from the database. Legacy unanimous workflows can be recovered from historical messages even when the latest phase is discussion; recovery is additive, records its migration time honestly, and stays paused. The current workflow remains authoritative if a file has been externally edited or publication is pending.

A member can request reopening the consensus after discussion, from a formal turn or a conversation-only lane:

```json
{"action":"request_revision","version":1,"reason":"New evidence requires changing the storage approach"}
```

The request must match the current proposal and concurrent generation fence. It requests reconsideration, not approval or implementation authority. It preserves `consensus_history` and stores the current proposal, checkpoint, feedback, contribution history, and check results in `revision_base`; `revision_request` identifies the requester and reason. The current draft and votes are cleared, active invocations are revoked, and existing files are not rolled back. A cancelled writer/check runner retains its lease until it actually exits; new formal readers wait for cleanup. The requesting turn itself is committed normally.

Agents then propose a new version and seek fresh unanimous approval. Previous completed milestones and approvals are not carried into the revised plan automatically. Existing artifacts may be inspected and reused through new checkpoints and judgments. A new confirmed record links its predecessor without modifying any old document.

Human `/revise <guidance>` aliases `/redirect` and sends the existing `{"type":"redirect","text":"..."}` request. `/consensus` requests the existing workflow response and displays the latest approved record and document history in the terminal. Both commands preserve wire compatibility; actual document creation and revision preservation require the upgraded coordinator. During ordinary discussion use `propose` or `object`, not `request_revision`. Chat-only configurations without a build workflow do not create formal consensus records.

## Shared implementation checkpoints

During `implementation`, any member holding the floor can improve the current dependency-ready milestone, including files written by peers. No member owns an exclusive coding partition.

```json
{"action":"contribute","version":1,"milestone_id":"T1","ready":false,"summary":"Draft interface; please challenge the boundary handling","files":["module.py"],"tests":"Not run; draft only"}
```

Use `ready: false` for partial work and `ready: true` to request milestone acceptance. Omitted `ready` defaults to false for `contribute`. The superseded `task_done` action is still accepted and defaults to `ready: true`, but it is no longer taught to backends; it never bypassed peer judgment. Use `contribute`.

Report actual changes and actual test results honestly. `files` must list existing workspace-relative files, with no absolute paths, parent traversal, or escaping symlinks. The list may be empty for work that only inspects or runs checks. Describe intentional deletions in `summary`.

The coordinator increments the milestone's `revision`, records the author and report in `contributions`, marks the milestone `judging`, and creates:

```json
{"milestone_id":"T1","revision":1,"author":"codex","ready":false,"approvals":[]}
```

This is the current `checkpoint`. Dependency milestones do not become available merely because their author claims completion.

## Immediate peer judgment and revision

In `judging`, every member other than the checkpoint author must inspect the actual implementation. This happens after each checkpoint, not only after the entire project has been written. Judgment turns must not modify project files; full-auto keeps tool and network capabilities available for inspection and research.

```json
{"action":"judge_pass","version":1,"milestone_id":"T1","revision":1,"evidence":"Read module.py and checked the boundary cases; this draft is sound"}
{"action":"judge_fail","version":1,"milestone_id":"T1","revision":1,"evidence":"module.py:12 drops zero; use an explicit None check and add a zero regression"}
```

Judgments must match both proposal version and checkpoint revision. Self-judgment, duplicate approval, stale votes, and votes about other milestones are rejected atomically.

A rejection is saved in the contribution's judgment history and shared feedback. It reopens the milestone and downstream dependencies, revokes review approvals, and gives the critic the next implementation turn. The critic may demonstrate the fix; the original author then becomes an eligible judge of that revision. Humans can override the next writer with `/next agent`.

After every peer accepts a checkpoint:

- `ready: false`: the milestone returns to pending for further shared work.
- `ready: true`: the milestone becomes done and releases downstream dependencies.
- The next writer rotates from the checkpoint author, rather than granting ownership to a milestone assignee.

Every revision must receive fresh judgments. Prior contributions and critiques remain in workflow state and the public conversation.

## Integrated review and acceptance

Once all milestones are accepted, every member reviews the entire integrated result in a non-writing `review` turn, including interactions with later changes. Full-auto permissions remain unchanged:

```json
{"action":"review_pass","version":1,"evidence":"Inspected the module, callers, and tests against every acceptance criterion"}
{"action":"review_fail","version":1,"milestone_ids":["T1"],"evidence":"Specific integration defect, location, and requested improvement"}
```

A failed review reopens the named milestones and their downstream dependencies. Repairs return through checkpoints and peer judgment before another integration review.

After unanimous integration approval, the coordinator executes `acceptance_checks` in `acceptance`. All must exit 0 for `completed`; models cannot submit a completed action. Failure preserves complete output and actual exit codes, reopens shared work, and repeats without a repair-attempt budget.

## Pausing, recovery, and floor control

Human presence is informational, not a scheduling condition. A transient `presence` event may contain an empty `names` list; losing the last human connection does not issue a control action, revoke the active turn, invalidate backend sessions, or stop subsequent turns and acceptance checks. Committed results are persisted even with no clients connected. Joining returns the current state and replays the complete committed conversation without changing pause state. Explicit pauses, blockers, errors, completion, and recovery retain their usual behavior.

The server process must remain alive for unattended work. Use `serve` with separate `join` clients; exiting a `join` client leaves the server running. Exiting the owner terminal of `start`, or shutting down the server itself, still closes the room and cancels active work.

```json
{"action":"blocked","reason":"Information or authority required from the human"}
```

A formal blocker preserves progress and pauses. In chatroom mode, ordinary `say` messages do not revoke work or reset the plan. `{"type":"redirect","text":"new guidance"}` (CLI `/redirect text`) revokes all active turns and reopens discussion. New guidance after completion or an explicit blocker also reopens discussion. Written files remain; interrupted replies cannot be committed. Serial mode retains its redirect-on-every-human-message behavior.

Confirmed usage exhaustion by **either Claude or Codex pauses the entire team**, in both chatroom and serial modes (`paused: true`, `reason: "quota"`). No discussion, consensus finalization, implementation, judgment, or acceptance checks are scheduled while a member remains limited. The coordinator revokes all active turns, including chat, writers, and checks; queued or late replies cannot publish or advance the workflow. Writers retain their lease until cancellation cleanup finishes. Failed and interrupted turns are revoked, but their known backend-session IDs and last acknowledged public cursors are retained for recovery. Committed history, documents, and filesystem side effects are preserved, not rolled back; required votes are never waived.

Generic HTTP 429/capacity errors, utilization warnings, and ordinary assistant text are not automatically classified as usage exhaustion. Codex's structured `UsageLimitExceeded` and Claude's native error plus rejected quota metadata are recognized, with explicit usage-exhaustion error messages as a fallback.

Each native rejection records a durable `agent.quota` event: `{speaker, quota: {backend, error, retry_at, retry_source, resets_at, limit_type}}`. Reset and retry times are UTC Unix seconds or `null`; `retry_source` is `provider` or `unknown`. `quota: null` clears the record. `state.quotas` additionally reports whether a recovery check is pending/in flight. Old fixed-delay deadlines are ignored on load, without deleting historical events. `/status` shows local reset/retry times, the timing source, and remaining wait.

Claude's rejected native `rate_limit_event` can supply `rate_limit_info.resetsAt` and `rateLimitType`. Metadata survives native assistant errors, explicit quota result errors, and explicit quota stderr exits. Codex's resident app-server is queried once with `account/rateLimits/read` after a quota failure. Its usage windows contain `usedPercent` and `resetsAt`; when multiple windows are exhausted, the latest reset is used. Missing reset information for any exhausted window, ambiguous buckets, credit exhaustion, and spend controls do not supply a reliable deadline. This best-effort metadata RPC has a 5-second timeout independent of model-turn timeouts; failure preserves the original quota error and requires manual recovery. Legacy serial Codex has no such RPC and retains an unknown deadline.

A finite, representable provider reset strictly later than detection schedules a check at that time plus a 30-second safety buffer. Missing, malformed, millisecond, or expired timestamps remain unknown; no fixed 5-hour fallback or textual time guessing remains. A reported reset is scheduling information, not a guarantee that quota is available.

An event-loop timer wakes the scheduler without a connected human or repeated account polling. After all interrupted turns finish, only limited members can run isolated availability checks (`phase: "recovery"`, chatroom lane `recovery`). Each check uses a fresh nonpersistent conversation instructed to return `[[PASS]]` without tools or project work. Its response is neither published nor interpreted as a workflow action, and it never clears or overwrites the working session's ID or cursor; normal full-auto permission policy remains unchanged. A successful response clears that member's quota. Discussion and work resume only when **all** limited members recover. Another quota rejection replaces the deadline with the newly reported reset or leaves it unknown.

Automatic checks do not override manual pauses, ordinary errors, document errors, or server restart. Recovered rooms require explicit resume even when a saved provider deadline has passed. `/retry [agent]` or `/resume` can request an early recovery check; they do not clear quota before success. A targeted retry cannot bypass another unavailable member. Human guidance and backend-session resets do not clear quota. Closing the server cancels its timer without deleting provider deadlines.

Invalid actions, missing formal actions, nonzero agent exits, enabled hard timeouts, and malformed transport pause the entire team with `reason: "error"` and revoke all active turns. The failed member retains its diagnostic. Unexpected check-runner exceptions also pause; nonzero check results reopen shared work as described above. Resolve the issue, wait for active turns to stop, then use `/retry [agent]` or `/resume`. Other errors during a quota recovery check also require explicit recovery. A stale concurrent action is a rejected decision, not a transport failure.

Hard timeouts default to `0` (disabled). `idle_warning_seconds` defaults to 120 and emits a transient `turn.idle` event with `speaker`, `turn_id`, `phase`, `idle_seconds`, and `text` after a period without observable output. One event is emitted per continuous silent period; activity rearms the observer. Stdout chunks (including non-public tool events and partial frames), stderr, and acceptance-command output count as activity. In-process adapters without activity callbacks are observed through their yielded reply deltas. Notices never contain the underlying private output, enter public model context, cancel work, change workflow state, or advance/invalidate session cursors. They are informational, not proof of failure. `/interrupt` remains available; setting `idle_warning_seconds = 0` disables notices.

Recovery loads the workflow snapshot saved atomically with its message and starts paused. Pending peer approvals are cleared after restart; restarting final review or acceptance requires fresh integration reviews. Existing contribution and feedback history is preserved.

The room's `state` event contains `turns` (count since this server started), not a remaining budget. Automatic scheduling is uncapped. `/next` advances one eligible agent turn and pauses with `reason: "step_complete"`. In judgment, the author and peers who already approved are ineligible. During automatic acceptance, use `/resume`.

Custom backends receive `AGENT_TEAM_PHASE=judging` for peer inspection, distinct from `review` for final integration review. They must honor read-only phases themselves.

## Backend-session synchronization

The default `context_mode = "incremental"` is supported by Codex and Claude adapters. Chatroom Codex uses resident `app-server` JSONL RPC (`initialize`, `thread/start` or `thread/resume`, `turn/start`, and streamed notifications). Claude uses resident `--input-format stream-json --output-format stream-json`, an initialized control channel, and successful `result` boundaries. The process does not exit to delimit a turn. Unexpected transport closure invalidates the invocation. Custom command and mock backends remain stateless. `context_mode = "full"` uses fresh nonpersistent conversations; Codex retains the server with a fresh thread, while Claude restarts its process.

An incremental-mode prompt includes a synchronization envelope:

```json
{"mode":"incremental","after":120,"through":145,"message_count":3}
```

`after` is the last acknowledged input cursor. `through` identifies the latest committed public message used for the invocation. IDs are event IDs and need not be consecutive. The transcript contains missing committed messages through that boundary, with original IDs and speakers. In concurrent incremental mode, it excludes separately acknowledged own replies already present in private history. A full rebuild uses `mode: "full"`, `after: 0`, and the entire public transcript without exclusions.

The latest human message is also included as a labeled reminder, not a newly delivered message. Current workflow state and reported changed file paths are supplied every turn. Those paths are not an exhaustive filesystem diff. Private notes must not override team state or replace public findings and evidence.

The coordinator persists a dirty marker before sending a new invocation and records a validated native session ID as soon as it is reported, even before the first model reply. Recording identity does not acknowledge input or publish a result. A completed public reply and its input acknowledgement are still committed atomically. Concurrent sessions use `cursor_mode: "input"` and record `known_own_messages` separately: committing a reply must not jump the cursor past intervening unread peer messages. Serial sessions retain their reply-boundary optimization. A `[[PASS]]` acknowledges only its input boundary without adding a public message.

Codex IDs are captured from `thread/start` or `thread/resume` results in resident mode, or `thread.started.thread_id` in serial mode. Claude IDs are checked on top-level session events; new IDs are preallocated UUIDs. Resumes must report the requested ID, and agents may not share IDs. Changed, malformed, or missing IDs fail closed.

Cancellation, quota rejection, uncertain completion, and malformed formal actions suspend the invocation while retaining its session ID and last acknowledged cursor. The next authorized invocation resumes that same session and includes a recovery notice: unpublished private decisions are not team authority, current workflow instructions supersede unfinished work, and actual file/test state must be checked before continuing. Unacknowledged messages may be delivered again with their original IDs; the cursor never advances merely because a failed invocation might have read them. A first-turn session with no acknowledged input may resume from cursor `0`. Dirty sessions loaded after a server restart follow the same rule, after explicit resume. A rejected concurrent decision recorded publicly can retain its clean session.

Resident connection errors still pause for explicit retry. A native session explicitly reported missing before any model/tool activity permits one full rebuild (`session.rebuilt`) in both chatroom and serial modes. Partial output, uncertain failures, changed identities, and repeated missing-session errors never trigger automatic replay. Configuration/context incompatibility, invalid saved cursors or IDs, and explicit `/reset-session` also require a fresh session. Provider transcript files are never deleted. Session IDs already erased by an older version cannot be reconstructed or guessed.

`/resume` on an already-running team is a no-op: it does not clear member scheduling markers, trigger idle peers, or start another model call. `/retry` remains an explicit request to retry failed members. Resuming a paused team preserves normal scheduling and the requirement that all limited members recover before discussion or work continues.

`state.sessions` reports IDs, `synced_through`, `generation`, `dirty`, and concurrent own-message acknowledgements. While dirty, the cursor is the previous committed boundary, not proof that the active invocation succeeded. The synchronization envelope identifies full versus incremental input; `turn.context` records each concurrent invocation's prompt hash. Serial mode also reports `floor.granted.context_mode` and fallback hashes in `session.rebuilt`.

The client request `{"type":"sessions"}` returns `session.state`. The `reset-session` control accepts an optional agent target, requires an idle floor, and pauses after discarding backend-session references. Public history, workflow progress, and project files remain intact. Use `/resume` or `/next` afterward.
