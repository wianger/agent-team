# agent-team

A shared conversation room where humans and coding agents (Claude Code, Codex) discuss an idea,
reach explicit unanimous consensus, then implement and judge each other's work in one workspace.

## Language

### The room and its participants

**Room**:
A durable conversation between a fixed set of members over one workspace. Its event log is the
authoritative record of everything that happened. Changing members or workspace means a new room.
_Avoid_: Session (that word names a backend session), conversation, channel

**Agent**:
A configured backend that can take turns — a `[[agents]]` entry in `team.toml`. Exists before any
room does, and owns the provider account whose quota can be exhausted.
_Avoid_: Bot, model, participant

**Member**:
An agent seated in a running room. Carries per-room state: turn history, votes, judgments.
Quota attaches to the agent; votes attach to the member.
_Avoid_: Participant, seat, player

**Coordinator**:
The member that runs acceptance checks and grants the floor. Not a privileged role otherwise —
every member can propose, challenge, implement, and review.
_Avoid_: Leader, orchestrator, manager

### Taking turns

**Turn**:
One member's slot to produce output. Bounded by `turn.started` and `turn.finished`.
_Avoid_: Round, step

**Phase**:
The workflow stage a room is in: discussion, planning, implementation, judging, review,
acceptance, completed. A phase contains many turns.
_Avoid_: Stage, mode, state

**Floor**:
Permission to speak, granted for the duration of a turn. Every turn holds the floor. Holding the
floor does not permit writing files.
_Avoid_: Token, mic, lock

**Write lease**:
Permission to modify the workspace, held by at most one member at a time and only during
implementation and acceptance turns. Strictly narrower than the floor: conversation-only turns
hold the floor and must not write.
_Avoid_: Floor, workspace lock, write lock

**Workspace lock**:
A guard preventing two teams from running against one workspace at the same time. Held for the
lifetime of a team, across processes, and unrelated to the write lease.
_Avoid_: Workspace lease, write lease

### Agreeing on work

**Proposal**:
A document under consideration: summary, milestones, acceptance criteria, and acceptance checks.
_Avoid_: Plan, spec, agreement

**Consensus**:
The state in which every member has approved the same proposal version. Recorded as a versioned
document before implementation begins.
_Avoid_: Agreement, approval, sign-off

**Objection**:
A member's recorded refusal of the standing proposal, carrying a reason. It revokes every approval
and is what licenses replacing another member's proposal. Silence is not objection, and an
objection is not a rejection of the work — it reopens the plan, not a checkpoint.
_Avoid_: Rejection, veto, disapproval, block

**Proposal version**:
One numbered draft of a proposal. Every new proposal is a new version, votes are cast against a
specific version, and a vote for a superseded version is refused rather than counted. The count of
versions is how a room notices it is not converging.
_Avoid_: Revision, draft number, iteration

**Milestone**:
A unit of shared work inside a proposal. Shared, not assigned: an owner is a focus, not a claim,
and any member may revise any milestone.
_Avoid_: Task, ticket, assignment, issue

**Checkpoint**:
A submitted revision of a milestone awaiting peer judgment. Its author cannot judge it; every
other member must.
_Avoid_: Submission, PR, review request

**Judgment**:
One member's verdict on another's checkpoint. A rejection carries evidence and reopens the work.
_Avoid_: Review, vote, approval

**Review**:
The whole-team read of the integrated result, after all milestones are done and before acceptance.
Distinct from judgment, which is per-checkpoint.
_Avoid_: Judgment, QA

**Acceptance criteria**:
Prose conditions a proposal must satisfy. Read by members, not executed.
_Avoid_: Requirements, definition of done

**Acceptance check**:
An executable command from the proposal, run by the coordinator. Completion means these passed —
not that every possible defect is absent.
_Avoid_: Test, verification, validation

### Context and recovery

**Event log**:
The append-only, ordered history of everything that happened in a room. Authoritative: any
disagreement between it and a backend session is resolved in its favour.
_Avoid_: Store, database, history file

**Backend session**:
A provider-side resumable conversation (Codex or Claude), identified by a `session_id` the provider
issues. Keeps the provider's own word deliberately. A disposable cache, never authoritative;
losing one costs a rebuild, not correctness.
_Avoid_: Session (unqualified), context, thread

**Incremental context**:
Sending a member only the messages it has not seen, rather than the whole transcript. The
alternative is full context, which resends everything.
_Avoid_: Session mode, missing messages, catch-up

**Delta**:
The wire-level representation of incremental context — the protocol token and the payload it
introduces. The transport term for what `incremental` names as a setting.
_Avoid_: Diff, patch, update

**Room state**:
Where a room is in its life: waiting for an idea, running, paused by a human, paused by a quota
limit, paused for input it cannot proceed without, or completed. One name for the whole room, not
a property of a member or a turn. A room is paused in every state but running.
_Avoid_: Status, mode, phase

**Quota limit**:
A provider refusing further work for an agent until a reset time. Pauses the entire room, not just
that member.
_Avoid_: Rate limit, throttle, ban
