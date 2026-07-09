# SDK Open TODOs

No open TODOs.

The previous entry — a plan to re-enable `schedule_callback` via
`SessionsToolsMixin` and a scheduled-callback CLI verb — is void.
`tools/src/tools/sessions.py` and `SessionsToolsMixin` were removed along
with the rest of the delegation surface; the agents no longer schedule
callbacks or hand work to another agent. If outbound/scheduled calling is
ever wanted again, design it fresh against the current stack rather than
reviving that plan.
