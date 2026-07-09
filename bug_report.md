# Bug Report — CoWork API

All line numbers refer to the original (unfixed) code. Every fix was verified
black-box against a running instance, including concurrent-request scenarios.

---

## Validation & business-logic bugs

### 1. Grace window allowed bookings starting in the past
- **File:** `app/routers/bookings.py:86`
- **Bug:** `if start <= now - timedelta(seconds=300)` allowed `start_time` up to
  300 seconds in the past. Rule 2 requires `start_time` strictly in the future
  with *no grace window of any size*.
- **Fix:** Changed the condition to `if start <= now`.

### 2. Missing minimum-duration and `end > start` validation
- **File:** `app/routers/bookings.py:89-94`
- **Bug:** Only the 8-hour maximum was enforced. A 0-hour booking
  (`end == start`) and even negative whole-hour durations (`end < start`)
  passed validation, violating rule 2 (min 1h, `end_time` strictly after
  `start_time`) and producing zero/negative prices.
- **Fix:** Added `end <= start → 400 INVALID_BOOKING_WINDOW` and
  `duration_hours < MIN_DURATION_HOURS → 400 INVALID_BOOKING_WINDOW`.

### 3. Back-to-back bookings rejected as conflicts
- **File:** `app/routers/bookings.py:50` (`_has_conflict`)
- **Bug:** Overlap used inclusive comparisons
  (`b.start_time <= end and start <= b.end_time`). Rule 3 defines overlap with
  strict inequalities; a booking ending exactly when another starts must be
  allowed. Legal back-to-back bookings got `409 ROOM_CONFLICT`.
- **Fix:** Changed to `b.start_time < end and start < b.end_time`.

### 4. `GET /bookings` — wrong sort order, wrong offset, hardcoded limit
- **File:** `app/routers/bookings.py:136-140`
- **Bug:** Three defects in one query: (a) sorted by `start_time.desc()`
  instead of ascending (rule 11); (b) offset was `page * limit` instead of
  `(page - 1) * limit`, so page 1 skipped the first `limit` items; (c)
  `.limit(10)` was hardcoded, ignoring the `limit` query parameter.
- **Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.

### 5. `GET /bookings/{id}` returned `created_at` as `start_time`
- **File:** `app/routers/bookings.py:166`
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote the
  correctly serialized `start_time` with the creation timestamp.
- **Fix:** Removed the overwriting line.

### 6. Refund tiers wrong at both boundaries
- **File:** `app/routers/bookings.py:200-206`
- **Bug:** (a) The 100% tier used `notice_hours > 48` after truncating to whole
  hours, so notice of exactly 48h–49h could fall into the wrong tier (spec:
  `notice ≥ 48h → 100%`). (b) The final `else` branch returned **50%** for
  notice < 24h, which must be **0%**.
- **Fix:** Compare `timedelta`s directly: `notice >= 48h → 100`,
  `notice >= 24h → 50`, else `0`.

### 7. Refund rounding wrong and inconsistent between response and RefundLog
- **Files:** `app/routers/bookings.py:208`, `app/services/refunds.py:15-17`
- **Bug:** The cancel response used Python `round()` (banker's rounding — 50%
  of 1001 → 500, spec says 501: half-cents round *up*), while `log_refund`
  independently recomputed the amount with float math and `int()` truncation.
  The two values could disagree, violating "the amount returned by the cancel
  response equals the amount stored in the RefundLog".
- **Fix:** Single integer half-up computation in the cancel handler —
  `(price_cents * refund_percent + 50) // 100` — and `log_refund` now stores
  exactly that amount instead of recomputing.

### 8. Duplicate registration returned 201 instead of 409
- **File:** `app/routers/auth.py:37-43`
- **Bug:** Registering an existing username in an org returned the existing
  user's data with 201 instead of `409 USERNAME_TAKEN` (rule 15). Also an
  account-enumeration/impersonation hazard.
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", …)`. Also catch
  `IntegrityError` on the user/org inserts so concurrent duplicate
  registrations return 409 (user) or fall back to joining the org (org race)
  instead of a 500.

### 9. Datetime offsets stripped instead of converted to UTC
- **File:** `app/timeutils.py:12-13`
- **Bug:** `dt.replace(tzinfo=None)` discarded the UTC offset without
  converting, so `10:00+06:00` was stored as `10:00` instead of `04:00` UTC
  (rule 1). This corrupted prices/conflicts/refund-notice for any
  offset-carrying input.
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

---

## Auth bugs

### 10. Access tokens lived 15 hours instead of 900 seconds
- **File:** `app/auth.py:50`
- **Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` =
  `timedelta(minutes=900)` = 15 **hours**. Rule 8 requires
  `exp − iat` = exactly 900 seconds.
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.

### 11. Logout never actually revoked tokens
- **File:** `app/auth.py:97` (with `revoke_access_token` at :85-86)
- **Bug:** Revocation stored the token's `jti`, but the check compared the
  `sub` claim (user id) against the revoked set — never a match, so a
  logged-out access token kept working (rule 8: logout must immediately
  invalidate the token).
- **Fix:** Check `payload.get("jti") in _revoked_tokens`.

### 12. Refresh tokens were reusable (not single-use)
- **File:** `app/routers/auth.py:81-93`
- **Bug:** `/auth/refresh` never invalidated the presented refresh token, so it
  could be replayed indefinitely. Rule 8 requires single-use refresh tokens
  (reuse → 401).
- **Fix:** Added an atomic `consume_token()` helper in `app/auth.py` that
  records the token's `jti` in the `revoked_tokens` table; `/auth/refresh`
  rejects with 401 if the token was already consumed. The primary-key
  constraint makes two *concurrent* refreshes with the same token yield
  exactly one success (see also bug 25 for why this lives in the database).

---

## Multi-tenancy / visibility bugs

### 13. Any member could read any booking in their org
- **File:** `app/routers/bookings.py:150-163`
- **Bug:** `GET /bookings/{id}` only scoped by org — a member could read other
  members' bookings. Rule 10: members may read only their own (else
  `404 BOOKING_NOT_FOUND`). The cancel endpoint had the ownership check; the
  read endpoint was missing it.
- **Fix:** Added the same check: non-admin + not owner → 404.

### 14. CSV export leaked other organizations' bookings
- **File:** `app/services/export.py:22-29, 48-52`
- **Bug:** With `include_all=true&room_id=<id>`, `generate_export` called
  `fetch_bookings_raw`, which queried by `room_id` **without any org filter** —
  an admin could pass another org's room id and export their bookings,
  violating rule 9.
- **Fix:** All paths now go through `_fetch_scoped`, which always joins
  `Room.org_id == org_id`; removed the unscoped `fetch_bookings_raw` helper.

### 15. Stale caches: report missed new bookings, availability missed cancellations
- **File:** `app/routers/bookings.py:121, 216-217` (caches in `app/cache.py`)
- **Bug:** Creating a booking invalidated only the availability cache (not the
  usage-report cache), so a cached report didn't reflect new bookings (rule 12:
  "immediately"). Cancelling invalidated only the report cache (not
  availability), so a cancelled slot still showed busy (rule 13).
- **Fix:** Create now also calls `cache.invalidate_report(org_id)`; cancel now
  also calls `cache.invalidate_availability(room_id, date)`.

---

## Concurrency bugs

(The `time.sleep()` "pause" helpers scattered through the code deliberately
widen these race windows. The fixes add proper synchronization rather than
removing the delays — the races are the bugs, not the latency.)

### 16. Lock-ordering deadlock between create and cancel notifications
- **File:** `app/services/notifications.py:24-35`
- **Bug:** `notify_created` acquired `_email_lock` → `_audit_lock` (nested);
  `notify_cancelled` acquired `_audit_lock` → `_email_lock` (nested, opposite
  order). A concurrent create + cancel could each grab their first lock and
  wait forever on the other — a classic ABBA deadlock that **hangs the whole
  service** (violates rule 16, liveness).
- **Fix:** The two resources are independent, so the locks are no longer
  nested — each is acquired and released sequentially. No nesting → no
  circular wait.

### 17. Double-booking and quota bypass under concurrent requests
- **File:** `app/routers/bookings.py:74-124` (`_has_conflict`, `_check_quota`,
  `create_booking`)
- **Bug:** Conflict and quota checks were check-then-act with a `time.sleep`
  between the read and the insert. Two simultaneous requests for the same slot
  both saw "no conflict" and both committed → double-booking (rule 3); same
  race let a member exceed the 3-booking quota (rule 4).
- **Fix:** A module-level `_booking_lock` serializes the critical section
  (conflict check → quota check → insert → commit), so the invariants hold
  under concurrency. Verified: 5 parallel requests for one slot → exactly one
  201 and four `409 ROOM_CONFLICT`.

### 18. Concurrent cancels produced double refunds
- **File:** `app/routers/bookings.py:178-218`
- **Bug:** The `status == "cancelled"` check and the status write were
  separated by `_settlement_pause()` (0.12 s) with no synchronization, and the
  RefundLog was committed *before* the status change in a separate
  transaction. Two concurrent cancels both passed the check → two RefundLog
  rows and two 200 responses (rule 6: exactly one RefundLog, concurrent-safe).
- **Fix:** The cancel critical section runs under the same `_booking_lock`,
  re-reads the booking status inside the lock (`db.refresh`), and commits the
  RefundLog and the status change in **one** transaction (`log_refund` no
  longer commits by itself). Verified: 5 parallel cancels → one 200, four
  `409 ALREADY_CANCELLED`, exactly one RefundLog.

### 19. Duplicate reference codes under concurrent creation
- **File:** `app/services/reference.py:17-21`
- **Bug:** `next_reference_code` read the counter, slept 0.12 s, then wrote
  back `current + 1`. Concurrent creations read the same value and issued the
  same code, violating rule 7 (uniqueness under concurrent creation).
- **Fix:** The read-increment is atomic under `_counter_lock`; the formatting
  pause happens outside the lock.

### 20. Lost updates in the room stats counters
- **File:** `app/services/stats.py:15-26`, `app/routers/rooms.py:103-115`
- **Bug:** `record_create`/`record_cancel` did read → sleep 0.1 s → write on a
  shared dict. Concurrent bookings lost increments, so
  `GET /rooms/{id}/stats` diverged from the values derivable from the bookings
  (rule 14). The counters also reset to zero on every process restart while
  the bookings survive in the database (see bug 23).
- **Fix:** The stats endpoint now derives count and revenue directly from the
  bookings table (`COUNT`/`SUM` over `status = 'confirmed'`), which by
  construction "always equals the values derivable from the bookings
  themselves" — race-free and restart-safe. The incremental in-memory
  `stats.py` service was removed along with its call sites.

### 21. Rate limiter undercounted under concurrent requests
- **File:** `app/services/ratelimit.py:18-26`
- **Bug:** The per-user bucket was read, then (after a 0.1 s sleep) appended
  and stored back. Concurrent requests each read the same bucket and stored
  back independently, losing entries — a user could exceed 20 requests/60 s
  without ever hitting `429 RATE_LIMITED` (rule 5).
- **Fix:** Trim + append + count run atomically under `_buckets_lock`; the
  bookkeeping pause moved outside the critical section.

---

## Robustness & restart-persistence bugs

(`docker-compose.yml` stores the SQLite database on a persistent volume, so
the data outlives the process — but several pieces of state lived only in
process memory. After a container restart the API contradicted its own
database. Reproduced by restarting the server against the same DB file.)

### 22. Malformed datetime strings crashed booking creation with a 500
- **File:** `app/routers/bookings.py:82-83` (parser in `app/timeutils.py:11`)
- **Bug:** `BookingCreateRequest` declares `start_time`/`end_time` as plain
  strings, and `datetime.fromisoformat` raises `ValueError` on garbage like
  `"not-a-date"` or `"2026-99-99T10:00:00"`. The exception was unhandled →
  `500 Internal Server Error`. The service must respond correctly to all
  requests (rule 16), and application errors must use the documented JSON
  shape.
- **Fix:** The parse is wrapped in try/except and raises
  `400 INVALID_BOOKING_WINDOW` (the documented booking-window error code).

### 23. Room stats reset to zero after a restart
- **Files:** `app/services/stats.py`, `app/routers/rooms.py:103-115`
- **Bug:** Stats lived in a process-local dict. After a container restart the
  bookings persist (volume-backed DB) but `GET /rooms/{id}/stats` reported
  `0 / 0`, violating rule 14 ("always equals the values derivable from the
  bookings themselves"). Verified: 3 bookings / 7000 cents before restart →
  `0 / 0` after.
- **Fix:** Covered by the fix for bug 20 — stats are now computed from the
  database on every request. Verified identical before and after restart.

### 24. Duplicate reference codes issued after a restart
- **File:** `app/services/reference.py:8` (`_counter = {"value": 1000}`)
- **Bug:** The reference counter restarted at 1000 on every boot while
  existing bookings persisted in the DB, so the first post-restart booking
  reused an existing code. Verified: two bookings both holding `CW-001000`.
  Rule 7 requires *every* booking's `reference_code` to be unique.
- **Fix:** The counter is lazily seeded from
  `MAX(bookings.reference_code) + 1` on first use (falling back to 1000 on an
  empty DB), under the same lock that fixes bug 19.

### 25. Logout / refresh revocation forgotten after a restart
- **Files:** `app/auth.py`, `app/models.py`, `app/routers/auth.py`
- **Bug:** Revoked `jti`s were kept in an in-memory set. After a restart, a
  logged-out access token (still within its 900 s lifetime) worked again —
  verified live — and a used refresh token (7-day lifetime) could be replayed.
  Rule 8: logout invalidates the token "for all further use"; refresh tokens
  are single-use.
- **Fix:** Added a `revoked_tokens` table (`jti` primary key). Revocation
  checks and refresh consumption go through the database, so they survive
  restarts; the PK constraint keeps concurrent refresh-reuse atomic
  (exactly one winner).

### 26. Export accepted cross-org / nonexistent `room_id` silently
- **File:** `app/routers/admin.py:65-73`
- **Bug:** `GET /admin/export?room_id=<other org's room>` returned
  `200` with an empty CSV. Rule 9 requires cross-org resource IDs to behave
  as non-existent → `404` on every code path.
- **Fix:** When `room_id` is supplied, the export endpoint verifies the room
  belongs to the caller's org and raises `404 ROOM_NOT_FOUND` otherwise.

### 27. Non-integer JWT `sub` claim crashed authenticated requests with a 500
- **Files:** `app/auth.py` (`get_current_user`), `app/routers/auth.py` (`refresh`)
- **Bug:** Both paths did `int(payload["sub"])` with no guard. A validly
  signed token whose `sub` is non-numeric (or missing) raised
  `ValueError`/`KeyError`, producing `500 Internal Server Error` instead of a
  clean `401`. Surfaced by a security fuzz pass signing a token with
  `sub="notanumber"`. Not exploitable without the signing secret, but it is a
  latent 500 and violates rule 16 (the service must respond correctly to all
  requests) and the "invalid token → 401" contract.
- **Fix:** Wrapped the conversion in `try/except (KeyError, TypeError,
  ValueError)` → `401 UNAUTHORIZED` in both `get_current_user` and the refresh
  handler.

### 28. Usage report served a stale cache after a room was created
- **File:** `app/routers/rooms.py` (`create_room`) — cache in `app/cache.py`
- **Bug:** The usage report is cached per `(org_id, from, to)` and invalidated
  on booking create/cancel, but **not** on room creation. Rule 12 requires the
  report to include *every* room in the org (including zero-booking ones) and
  to "reflect the current state immediately." Reproduced live: request a
  report (caches it), create a new room, request the same report again — the
  new room was missing because the stale cache was returned.
- **Fix:** `create_room` now calls `cache.invalidate_report(admin.org_id)`
  after committing the new room, so the next report recomputes and includes it.
  Verified: report goes from 1 room to 2 immediately after room creation.

---

## Adversarial pass — verified safe (no change needed)

A full security sweep (`sec_probe.py`) confirmed the following are already
handled correctly, so no code was changed for them:

- **JWT:** `alg=none`, tampered/garbage tokens, wrong-secret forgeries,
  expired tokens, and refresh-token-as-access are all rejected with 401
  (`jwt.decode` pins `algorithms=[HS256]`).
- **Authorization source:** role and org are always read from the database
  (`get_current_user` → DB), never trusted from JWT claims, so a stale token
  cannot escalate privilege or cross tenants.
- **IDOR / multi-tenancy:** cross-org read/cancel/availability/stats/booking
  and member-reads-another-member all return 404; admin-only endpoints return
  403 for members — verified on every endpoint.
- **Input fuzzing:** a battery of malformed datetimes (empty, unicode digits,
  out-of-range components, bad offsets, over-long fractional seconds),
  negative and huge room ids, and pagination extremes never produce a 500.
- **CSV export:** only system-controlled fields are emitted (id, reference
  code, ids, timestamps, status, price) — no user free-text, so no CSV
  formula-injection vector.
- **State consistency:** cancelled bookings are excluded from availability,
  stats, and the usage report; date-boundary filtering for availability is
  correct; half-up refund rounding holds on odd prices (50% of 1001 = 501).
