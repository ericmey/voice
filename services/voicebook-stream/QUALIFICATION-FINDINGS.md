# voicebook-stream — qualification findings (tracked)

## F1 — `outcome=disconnect` misclassifies successful streaming delivery

**Class:** observability. **Severity:** non-blocking for component qualification
(automated GO stands); **MUST be fixed/bounded before deployment-observability
acceptance** (Yua, 2026-07-23). **Status: OPEN.**

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
