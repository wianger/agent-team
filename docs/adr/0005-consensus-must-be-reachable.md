# Consensus must be reachable, not merely required

Requiring unanimous approval of one proposal version says what consensus *is*, but nothing
guarantees a room can ever get there. Any member could replace a standing proposal at any time,
which bumped the version and wiped every existing approval, at no cost to the replacer. Two
members disagreeing in turn could therefore trade proposals indefinitely, each one resetting the
other's vote, with no member behaving incorrectly and no state that a reader could distinguish
from productive discussion.

[ADR-0004](0004-no-application-imposed-limits.md) declined to bound loops like this, on the
grounds that provider quota is the real bound. That reasoning holds for work that is progressing.
It does not hold here: the loop makes no progress, and "quota is the bound" means the room drains
an account and stops without ever reaching implementation. That is the observed failure, not a
hypothetical one.

Three changes make consensus reachable:

- **Replacing another member's standing proposal requires an objection first.** An objection
  carries a reason and is recorded, so displacing someone else's proposal costs a turn and leaves
  evidence. Refining your *own* standing proposal stays free, because responding to discussion is
  the behaviour we want, and a livelock needs two different members by definition.
- **Proposing approves the proposal.** A member who just authored a proposal is not undecided
  about it, and the separate confirming vote bought nothing but a turn. Every *other* member still
  approves explicitly, so consensus remains unanimous and explicit.
- **`proposal_version_limit` (default 5, `0` disables) pauses the room for a human** once that
  many proposals have failed to reach consensus.

## Consequences

A member that wants to replace a proposal must state why, which makes the disagreement visible in
the log instead of leaving a silent version bump. Consensus costs one turn less, which matters
because turns are quota.

The version limit is the one bound this project imposes on a loop, and ADR-0004's objection to
caps mostly does not apply to it: it does not truncate work at an arbitrary boundary, because a
room that has burned five proposals without converging is not producing work to truncate. It
pauses for a human rather than failing, it is configurable, and `0` disables it, matching how
every other bound here behaves. ADR-0004 stands as written; this records the case its reasoning
did not cover.
