# A quota limit on any member pauses the whole room

When one agent exhausts its provider quota, the room could continue with the remaining members.
We pause the entire room until every limited member passes an isolated recovery check.

Continuing without a member silently changes the rules the room agreed to: consensus requires
unanimous approval and every checkpoint requires judgment from every other member. A room that
proceeds a member short is either blocked at the next vote anyway, or is quietly redefining
unanimity to mean "whoever is currently available" — which would let work be approved by a subset
and discovered later. Pausing makes the degraded state explicit and recoverable.

## Consequences

One exhausted account stops all progress, including work that member was not needed for. Recovery
is scheduled from the provider's reported reset time plus a buffer; an unknown reset time requires
an explicit `/retry` or `/resume`. Required votes and existing file changes survive the pause.
