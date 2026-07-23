# voicebook-stream — qualification findings (tracked)

## F1 — `outcome=disconnect` misclassifies successful streaming delivery

**Class:** observability. **Severity:** non-blocking for component qualification
(automated GO stands); **MUST be fixed/bounded before deployment-observability
acceptance** (Yua, 2026-07-23). **Status: RUNTIME-VERIFIED — fix accepted on
second read; image `cu128-f1` (sha256:3b28aa8102d6…) built from clean f086999
with exact context-mirror; real-uvicorn 2.3 regression PASSED (complete
stream → outcome=ok — the exact case that logged disconnect on the pre-F1 image;
real cancellation → outcome=disconnect + lease recovery). Pending only Yua's
final deployment-observability sign-off acceptance.**

**Resolution (2026-07-23):** `ReservationStreamingResponse.__call__` now wraps
`send` and marks the body complete ONLY after the terminal
`http.response.body(more_body=False)` send returns; `outcome=disconnect` is set
only when a disconnect was observed AND the body did not complete. Red-proofed
against all four event orders (see `tests/test_routes.py::test_f1_*`): full
delivery→ok; disconnect-before-final→disconnect; final-send-raises→post-header
error with lease/backend cleanup; clean completion→ok exactly once. The order-1
test was confirmed to FAIL against the pre-fix code and pass after. Gates:
42 passed, ruff clean, pyright 0. Remaining: second-read acceptance, then a new
immutable image + targeted runtime regression before deployment sign-off.

**Observed:** 2026-07-23, runtime qualification T5/T6. Under ASGI spec_version
2.3 (uvicorn serves 2.3 over HTTP), a streaming client that closes the
connection *after* receiving the full body still causes
`ReservationStreamingResponse.__call__` to observe `http.disconnect` via
`watching_receive()`, so the terminal correlation metric records
`outcome=disconnect` for a request that delivered **HTTP 200 and complete
audio**. Seen live: request `702aa7b9…` (the T6 recovery request) delivered 200
+ full audio but logged `outcome=disconnect`.

**Impact:** the terminal success/disconnect classification can mark a
fully-successful streaming request as a disconnect. Engine and route
correctness are unaffected (200 + full audio delivered). This is an
observability defect only — but it corrupts the exact metric a deployment would
use to distinguish real client aborts from clean completions.

**Location:** `src/voicebook_stream/app.py` —
`ReservationStreamingResponse.__call__`, the `disconnected["seen"]` / `outcome`
logic (the 2.3 branch that sets `outcome=disconnect` when `__call__` returns
normally *and* an `http.disconnect` was observed).

**Required before deployment-observability acceptance:**
1. Reproduce the exact ASGI event order that separates (a) a genuine mid-stream
   client abort from (b) a client closing after full delivery — specifically
   whether all body bytes (and the final empty body message) were sent before
   `http.disconnect` arrives.
2. Fix/bound the terminal metric so a fully-delivered stream records success
   (e.g. `outcome=ok`) and only a genuine pre-completion abort records
   `outcome=disconnect`.
3. Add a discriminating test for BOTH orders (full-delivery-then-close vs
   mid-stream-abort) so the classification is red-proofed in each direction.
