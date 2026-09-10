# No application-imposed round, length, or context limits, and timeouts off by default

Systems that drive LLMs in a loop almost always cap rounds, output length, and total context, and
impose hard turn timeouts, to bound cost and prevent runaway loops. We impose none of these by
default: `turn_timeout`, `work_timeout`, and `acceptance_timeout` are all `0`, and there is no round or
transcript cap.

Every such cap we considered would truncate correct work at an arbitrary boundary rather than
prevent incorrect work. The real bounds already exist and are enforced elsewhere: provider quota,
provider context windows, and available memory and disk. An application-level cap on top of those
mostly produces work that is abandoned just before it finishes, and a timeout in particular cannot
distinguish a stuck agent from a slow correct one.

## Consequences

A genuinely stuck member will wait indefinitely rather than fail. The mitigation is informational,
not enforcing: a notice after `idle_warning_seconds` (120 by default) without observable output,
which does not cancel work or assert that the agent is stuck. Humans interrupt deliberately with
`/pause` or `/interrupt`. Operators who want hard bounds opt into the timeouts explicitly.
