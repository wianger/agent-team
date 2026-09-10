# One workspace write lease per room

Members work concurrently on shared milestones, so the obvious design is to let each write in its
own git worktree and merge. We instead give the whole room a single write lease, held by at most
one member at a time, and only during implementation and acceptance turns.

Merging concurrent agent edits requires either conflict resolution the agents are bad at, or a
human arbitrating — and both undermine the point of reciprocal peer judgment, which is that
members read the *actual* integrated result rather than a proposed patch. A single writer keeps
one true workspace state that every judgment and acceptance check refers to unambiguously.

## Consequences

Implementation is serialised even though discussion is concurrent, so throughput is bounded by one
writer regardless of team size. Conversation-only turns may research but must not write, which the
member prompt states explicitly and the resident adapters enforce by revoking tools. The critic
can take the next write turn, which is what keeps serialisation from becoming a bottleneck on
rejection.
