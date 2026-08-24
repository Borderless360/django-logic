# Changelog

## [Unreleased]

## [0.17.0] — 2026-08-24

The cleanup after the pull cut (#221): the words now match the engine,
one copy exists of what had four, and two mechanisms that could not do
their jobs were replaced by ones that can. Library size shrinks; the
consumer API is unchanged except where noted.

### Fixed

- **The worker wake-up works on psycopg 3** (#227). `_wait_for_work`
  called `poll()` and `notifies.clear()` — psycopg 2 methods — on the
  psycopg 3 connection the project pins, so every wait raised into a
  bare `except` and slept a second full poll interval; a notification
  made pickup slower than a plain poll. psycopg 3 (>=3.2) now waits
  inside `notifies(timeout=..., stop_after=1)`; psycopg 2 keeps
  `select` + `poll()` and drains a notification that arrived during
  earlier statements. `LISTEN` is issued once per connection. Validated
  against PostgreSQL 17: the wait returns at the notification, not the
  timeout. Two new tests pin it (one live-NOTIFY, one degrade-to-sleep).
- **Migration 0008's docstring** no longer describes two columns that
  migration 0009 removed (#233). The file rename waits for the 1.0
  squash: renaming an applied migration ghosts it in every consumer's
  `django_migrations`.

### Changed — `timeout=` now means what it says (#229)

- The worker enforces the declared budget: every attempt runs in its
  own forked attempt process, and one that runs past its `timeout=` is
  killed. The kill releases the attempt's row lock with its connection,
  one `[timeout]` error is recorded on the row, the claim's retry wait
  paces the next attempt, and at `MAX_ERRORS` the stuck finalizer ends
  the row in `failed_state`.
- Removed with it: `abandon_timed_out_attempt`,
  `watchdog_stale_attempts`, the watchdog's safety-net step, and
  `retry_status`'s declared-budget branch with `RETRY_SLACK`. The
  watchdog could only reach an attempt whose row lock was already gone
  (side-effects run inside that lock), so it charged budgets it could
  not enforce; five fixes across four releases guarded that scan, and
  the third fix on one defect class is a design cut. A live long attempt
  stays protected by the row-lock probe (0.15.0).
- Behaviour change: `timeout=` kills attempts instead of only charging
  them, and enforcement exists only where an attempt process exists —
  sync mode runs in the caller's thread, unbounded. The consumer census
  found zero `timeout=` declarations. The `timeout_seconds` column stays.

### Changed — background enqueue no longer re-runs conditions (#231)

- `BackgroundTransition.change_state` re-ran the conditions and
  permissions the resolver had already evaluated, before taking the
  lock, so the re-check closed no race and cost every enqueue a
  duplicate query burst. The synchronous path never had it. The guard
  that matters is unchanged: `_ensure_db_state_in_sources` runs under
  the lock. A condition that flips between resolution and enqueue now
  enqueues — the behaviour the synchronous path already had.

### Changed — one copy of what had four (#228, #225, #230)

- `commands.write_failed_state` is the one savepointed `failed_state`
  write. Both synchronous `fail_transition`s and both worker terminal
  paths call it; the finalizer's divergent `set failed_state=` log line
  becomes the standard `SET_STATE` event.
- `runner._complete_terminal_failure` is the one terminal completion for
  the worker attempt path and the stuck finalizer
  (`runner._finalize_stuck_row`).
- One tree walk (`_find_transition`) answers which background transition
  a row names; the owner match wins during the walk, and the warning for
  a recorded-but-unmatched owner no longer misreports a renamed
  transition as a missing class.

### Changed — one module per concern (#232, #235)

- `django_logic/conf.py` owns every `DJANGO_LOGIC` key: one reader per
  key, one number validator, one bool validator, one place per default.
  `django_logic/background/settings.py` is gone; the background boot
  gate (with the two pull-mode deployment checks) lives in
  `background/apps.py`.
- `django_logic/background/dispatch.py` is gone: `sync_execution` and
  the `sync_mode()` reader live in `conf`, the two-branch handoff is
  inlined into `BackgroundTransition.change_state`, `in_flight` lives
  beside `retry_status` in `models.py`, and `retry_pending` is
  `safety_nets.run_pending`. Every documented import path keeps working
  through the lazy public map. `in_flight` no longer special-cases an
  install without the background app: the background engine is default
  functionality, so the probe reads the row, always.
- One name for the forked process: it is the **attempt process**,
  everywhere. `_run_attempt_process`, `_wait_for_attempt_process`, and
  `_record_error_if_uncompleted` replace the child/death names from
  0.16.0, and the prose says worker/attempt process instead of
  parent/child.
- Dead names cleared: `process._RESERVED_KWARGS`, `run_pending`'s
  never-passed `queues=`, `run_once`'s never-used `isolate` default,
  `observability.task_label`, two unused imports. Kept against the
  issue's suggestion: `State.unlock`'s token-less force-release — it is
  pinned by two tests and is the documented manual repair path for the
  defer-unlock rollback trade-off.

### Changed — the words match the pull engine (#222, #223, #224, #234)

- No comment, docstring, test, or document still says the periodic
  starter re-dispatches rows, names `STARTER_QUEUE` (the README's
  install example carried it), or pins Celery `acks_late`. The claim's
  `WHERE` clause is the retry rule and the prose now says so.
  `BACKGROUND_TRANSITION_ANALYSIS.md` is marked as the broker-era
  historical record; `PULL_WORKERS.md` is the current one. The README
  carries the four-type lock/gate/chain contract table until 1.0
  unifies the types.
- `__all__` stops advertising the command classes (`Conditions`,
  `Permissions`, `SideEffects`, `Callbacks`); consumers declare lists of
  functions, and direct imports from `django_logic.commands` (and the
  top level) keep working.
- Test files are named for the contract they pin, not the review that
  found the defect; every assertion kept. `test_issue_fixes_0_12.py`
  split into `test_write_failure_accounting.py` and
  `test_definition_validation_pins.py`.
- Comments in `process.py` and `background/models.py` state the rule in
  one or two sentences; the incident history stays here in the
  changelog.

### Changed — the follow-up simplification pass

- One copy of what still had two: the stuck finalizer decodes kwargs
  through `_decode_kwargs`, `_restore` loads the recorded class in one
  shared block, `_handle_failure` builds one outcome, and the
  "has no transition" message is built once. `db_safe_text` loses a
  `limit=` parameter nothing passed.
- `BACKGROUND_EXECUTION='celery'` no longer has a bespoke removal
  message: the migration it guided is complete, and an unknown mode
  still fails loudly at boot naming the valid ones.
- Comments and docstrings across the engine and the testing package
  state the rule and stop narrating the defect history that produced it
  (the changelog owns that history). The mode-durability rule is written
  once; version-number citations in prose are gone.

### Added

- `docs/recipes/long-jobs.md` — one row per chunk: the supported shape
  for a job that does not fit one attempt's budget (#219). Each chunk
  commits its own attempt, so an interruption loses one chunk, not the
  job, and every engine guarantee holds unchanged.
- `TODO.md` carries the full 1.0 plan: the one-transition-type contract,
  the bridge removals, the migration squash, and the lock-identity
  rework (database alias + concrete-model identity in one key change).

## [0.16.0] — 2026-08-21

The design cut (#217): workers pull committed rows from the database, so
the broker mirror — and every mechanism that reconciled it — is gone.
One incident class (the message and the row disagree) produced five
shipped defects across four releases; this removes the class instead of
guarding it again.

### Changed (breaking) — the pull engine

- **Workers claim rows from the database.** `BACKGROUND_EXECUTION`
  defaults to `'pull'`: enqueue commits the row and fires one
  payload-free `NOTIFY`; a worker claims with
  `SELECT FOR UPDATE SKIP LOCKED` and runs the unchanged execute path.
  The claim's filter is the retry rule — a failed row reappears after
  `RETRY_MINUTES` on its own, and a crashed worker's row is claimable
  the moment its connection dies (faster than the starter it replaces).
  Run one `manage.py dl_worker --queues <names>` process per queue
  group. The loop runs the safety nets once a minute, so nothing is
  scheduled anywhere.
- **`'celery'` mode is removed** and reports its removal with the
  migration steps at boot. Celery is no longer a dependency; the
  `[celery]` extra stays as an empty alias so old pins resolve.
  Consumers switch by setting `'pull'`, replacing their Celery worker
  and beat lines with `dl_worker` processes, and draining the old broker
  queues once.
- **Removed with the mirror:** the dispatcher's broker half, the
  periodic starter task, the beat schedule and `STARTER_QUEUE` (a
  leftover key is reported by `django_logic.W004`), the
  `django_logic.W002` check, and 0.15.0's dispatch claim, counter,
  ceiling and refund (`last_dispatched_at` and `dispatch_count` drop in
  migration 0009) — a row that nothing consumes now simply waits,
  visibly, and `detect_stuck_transitions` names it and its queue once it
  is older than the retry window.
- **Every attempt runs in a forked child of the worker.** A crash in
  consumer code — a hard exit, a segmentation fault, the platform's
  memory killer — kills the attempt process, not the worker, and the
  parent records it as an error on the row. A crashing attempt therefore
  gets the same paced, bounded retries as a failing one, ending in
  ``failed_state`` at ``MAX_ERRORS``; before, a crash left no error
  anywhere and recovery depended on the platform restarting the whole
  worker (with its repeated-crash backoff parking the queue group).
- The safety nets live in ``django_logic.background.safety_nets`` as
  plain functions; ``retry_pending()`` still runs one inline retry pass
  for tests. The declarations, the runner, sync mode and the testing
  package are unchanged — no consumer process definition changes.

### Fixed (found validating the release)

- The Heroku matrix under pull found that a crashing attempt killed the
  whole worker and the platform's restart backoff parked its queue group
  — hence the forked child above (#218).
- The consumer re-vendor review found two more (#220): a died attempt's
  error could dirty a row another worker had already completed (the
  record is now one conditional statement), and a sustained backlog
  starved the safety nets (the drain loop now breaks out when they are
  due).

## [0.15.0] — 2026-08-20

Six consumer-reported issues from the gv coupled-core migration — five found
reviewing the integration app port (gv#9594), one observed live on the gv
staging broker right after it. No breaking changes: every declaration and
call that worked on 0.14.x works unchanged.

### Added

- **A failure can say it is permanent** (#209). A background side-effect
  that raises `django_logic.background.PermanentFailure` — or an exception
  type the transition lists in `no_retry_on=(...)` — takes the terminal
  path on that first attempt: `failed_state` is written, the row completes,
  `failure_callbacks` run. Before, a business refusal ("no record matched",
  "the rule says no") waited out `MAX_ERRORS × RETRY_MINUTES` before the
  user saw it, and consumers routed around the retry policy with
  target-less actions plus per-outcome verdict transitions. Ordinary
  exceptions keep their retries — that asymmetry is the point.
- **Two testing helpers the reference consumer kept hand-rolling** (#214).
  `django_logic.testing.open_transition_message(instance, process_name,
  transition_name, started_minutes_ago=...)` stands up a coherent
  uncompleted `TransitionMessage` for tests that pin the one-uncompleted-row
  gate (gv wrote that eight-field row by hand in three files, each slightly
  differently). `django_logic.testing.record_driven_transitions()` records
  which actions a test block drove and diffs against a process's
  declarations (`record.undriven(MyProcess)`). Unlike the runtime coverage
  subsystem 0.14.0 removed, this is a test-scope context manager with no
  engine seam, no setting, and a consumer who asked for it.

### Fixed

- **A live long attempt is no longer called stranded** (#210). Past the
  retry window, `retry_status` now probes the row lock (savepointed
  `select_for_update(nowait=True)`, never blocking) before answering
  `STRANDED`. Before, an attempt that ran quietly for more than
  `max(RETRY_MINUTES × (MAX_ERRORS + 1), 15)` minutes with no declared
  `timeout=` made the gates tell the operator "nothing is retrying it —
  complete the row" while a worker held the row and the starter was
  re-dispatching it. The queue-backlog ambiguity remains and the stranded
  message still names it; the dangerous half — a human completing a live
  row — is gone.
- **The starter no longer duplicates dispatches for long attempts** (#211).
  `retry_stale_transitions` skips a row a worker holds. Before, every
  attempt longer than `RETRY_MINUTES` drew one no-op broker message per
  tick for its whole duration (harmless via the lock, but one wasted
  message and worker pickup per row per tick). A crashed attempt holds no
  lock, so re-dispatch is as fast as before — faster than a timeout-budget
  filter would have been.
- **Publishing for one row is bounded, and a queue with no consumer is
  named** (#215). The starter now claims `last_dispatched_at` on the row
  before it publishes — a compare-and-set, so a row costs at most one
  broker message per retry window instead of one per tick, and the primary
  dispatch counts as the first (migration 0008 adds the marker and a
  `dispatch_count`; instant on PostgreSQL). A publish that raises gives its
  count back — the window stays spent, so a broken broker is asked once per
  window, but the ceiling counts only messages the broker really took
  (found in review, by Cursor Bugbot). A row published 60 times
  that never once started stops being re-dispatched, and
  `detect_stuck_transitions` reports it by name and queue — "does that
  queue have a consumer?" — instead of the silence that let five stalled
  rows put ~7,000 messages a day on a staging broker. Alert-only on
  purpose: a deep backlog clears and the queued copies run the row;
  finalizing would fail work that would have completed. Claims do not
  touch `modified`, so `retry_status` still answers stranded for exactly
  these rows.
- **The cleanup sweep keeps failure forensics** (#213).
  `cleanup_completed_transitions` now keeps the newest terminal-failure row
  per instance and process instead of deleting it on the same
  `CLEANUP_DAYS` clock as successes. That row is the only explanation for
  an instance parked in its `failed_state`. One row per parked instance:
  bounded, and no new setting. A new `ended_in_failure` flag on the row
  (migration 0008) is what marks a failure — an `errors_count` comparison
  cannot, because a permanent failure completes at one error and a retried
  success can carry several (found in review, by Cursor Bugbot).

### Documented

- **The window between the worker's commit and its callbacks** (#212). A
  worker killed there loses callbacks and `next_transition` forever — this
  is the design's documented best-effort boundary (see the crash table in
  `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md`), kept rather than fixed:
  recovery machinery for hooks is the kind of self-guarding growth the
  release policy exists to stop, and a re-run could not restore an
  in-memory verdict anyway. The README now says what the consumer owes: a
  callback that applies a recorded decision needs a periodic re-check
  behind it, or the follow-up becomes its own `BackgroundTransition`.
- **The Voice rule covers writing to people** (#208, folded in). A summary,
  a pull-request description, a review reply, or a chat message must stand
  on its own: say the thing, not a self-coined label for the thing, and
  define any unavoidable name in the same message. Added to `CLAUDE.md`
  and the Cursor rule.

### Deferred, deliberately

- #184 (the lock key omits the database alias) and #186 (MTI/proxy models
  sharing a state column alias the lock key) stay open. Both change the
  lock key's shape, which is shared-cache state: old and new processes
  disagree about what is locked for the length of a rolling deploy. That
  fix wants a release of its own with a drain note, not a seat in a
  correctness release.
## [0.14.1] — 2026-08-14

Documentation only. No engine change, so upgrading from 0.14.0 changes no
behaviour.

### Changed

- **Docs: declarations are specifications** (#205). The README states the
  design principle — write every process and usage out in full; duplication
  in declarations is acceptable and usually preferable — with a
  before/after example, the data-dependent-outcome ("verdict") callback
  pattern next to the one-in-flight gate, a note in the nested-processes
  recipe, and the rule in CLAUDE.md for AI tools.

## [0.14.0] — 2026-08-14

### Removed (breaking) — the second diet

Every removal below had zero consumers in gv, the reference consumer, and
zero consumers in the Heroku harness beyond tests of the feature itself.
This is the follow-through on the overengineering review of 2026-08-13
(engine grew from 6,305 to 7,621 lines in three releases; most of the
growth defended the engine's own machinery).

- **The transition-coverage subsystem** (`django_logic.coverage`, the
  `transition_observers` seam in `process.py`, the
  `TRANSITION_COVERAGE_LOG` setting and its boot validation, both
  `ready()` activations, the README section). #132/#146 surface;
  nobody ever set the knob. A leftover setting key is reported by
  `django_logic.W004` with migration advice.
- **Per-transition `lock_timeout=`** on `Transition`. Zero declarations
  across 175 consumer transitions; the sweep that motivated it died in
  0.12.0. The global `DJANGO_LOGIC['LOCK_TIMEOUT']` is the only TTL, and
  `State.lock()` takes no arguments again. A leftover `lock_timeout=`
  kwarg raises `ImproperlyConfigured` at class creation.
- **The `failure_side_effects` hook family** (`FailureSideEffects`
  bundle, the `failure_side_effects=` kwarg, the tracking slot and the
  `assert_failure_side_effects_ran` scenario assertion). Consumers
  declared it never (0 uses vs 41 `failure_callbacks`), and the bundle
  hosted its own savepoint fix chain (#138, #189) for hooks nobody had.
  On failure the engine now writes `failed_state`, unlocks, and runs
  `failure_callbacks`. A leftover `failure_side_effects=` kwarg raises
  `ImproperlyConfigured` at class creation.
  `TransitionMessage.failure_side_effect_error` stays: it records a
  rejected `failed_state` write.
- **`to_json`** from `django_logic.testing` (zero consumers; `snapshot()`
  returns a dict that `json.dumps` handles).
- **The separate removed-settings check function** — merged into
  `check_no_unknown_settings`, so one function now owns "the engine does
  not read this key". The two reports keep their own ids:
  `django_logic.W003` still carries the per-key migration advice for a
  removed key, and `django_logic.W004` still reports a typo. They stay
  separate because the W004 hint tells you to silence W004 when you keep
  extra keys in `DJANGO_LOGIC` on purpose — a shared id would have hidden
  the migration advice from everyone who followed it, and would have
  turned `check --fail-level WARNING` red for anyone who had silenced
  W003. No settings change is needed on upgrade.

### Added

- `CLAUDE.md` now carries the **architecture rule** (the cache may lie
  briefly; the database row never lies; everything recoverable recovers
  from a row) and an **anti-spiral release policy**: no hardening without
  a consumer-reproduced failure, adversarial self-review capped at one
  pass, and a third fix on one defect class triggers a design cut.

### Fixed

- **`beat_schedule()` schedules cleanup by crontab (03:17), not a 24-hour
  interval** (#203, reproduced on both gv Heroku apps). Interval entries
  count from beat start-up and the default scheduler state lives on disk,
  so a beat that restarts on every deploy — or daily, on platforms that
  cycle dynos — never reaches a day-scale interval: completed rows piled
  up 9–10 days deep while the short-interval tasks ran fine. The
  ``cleanup_seconds`` keyword becomes ``cleanup_schedule`` (any Celery
  schedule value).

### Changed

- The testing-guide scenario catalog is five canonical scenarios; the
  other shapes stay pinned in `tests/` and the guide points there.
- Kept, deviating from the diet plan: `retry_pending` and
  `unbind_model_process` stay public — both are documented testing API
  with harness consumers, and demoting them saves nothing.

- **Comments, exception text, and a handful of internal names** now use
  ordinary English. Enqueue vs execute, not phase 1 / phase 2. Ticket
  numbers stay in this changelog, not in `.py` comments. The README,
  testing guide, logger notes, and nested-process recipe use the same
  words. Public APIs that already made sense are unchanged
  (`BackgroundTransition`, `TransitionMessage`, `in_progress_state`,
  `in_flight()`). `TransitionMessage.retry_status()` replaces
  `in_flight_liveness()`; `RETRYING` / `STRANDED` / `RETRY_SLACK`
  replace `LIVENESS_*`. `_enqueue_atomic` replaces `_phase_one_atomic`.
  Stranded-row exceptions no longer mention `W002`, `retry horizon`,
  or `re-drive`.

- **The plain-English pass now covers the whole project**, not just the
  engine: the 44 test modules that still carried the dialect, and
  `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md`, which used to exempt
  itself from the vocabulary. `CLAUDE.md` states the rule as simplified
  English (ASD-STE100) with a table of retired words, and
  `tests/test_voice.py` enforces that table over the library and the
  current documentation. This changelog is deliberately out of scope: it
  is a historical record, so it keeps naming the words and the APIs that
  shipped at the time.

  Three facts in the design document were wrong and are corrected: the
  step count in the execution walkthrough, the check id for a removed
  settings key, and what `django_logic.W002` reports (it reports
  safety-net tasks missing from the beat schedule; it cannot tell you
  whether a queue has a worker).

### Changed (operators — check your log queries)

- **The worker's first log line reads `Execute Start`, not `Phase2
  Start`.** Everything after it is unchanged (`SideEffect`, `Set State`,
  `Complete`). Update any alert, saved search, or dashboard that matches
  on the old text. Nothing else in the log format changed.

## [0.13.1] — 2026-08-10

Five fixes from the 0.13.0 adoption review (raised by the gv consumer as
#194–#197) plus #192, the sync analog the 0.13.0 review deliberately deferred.
One is a 0.13.0 regression; the rest harden the release's new surfaces. An
adversarial review pass over this release's own diff then confirmed nine gaps
in the first cut — the largest being that the staleness horizon used the
wrong liveness signal — all fixed and folded into the bullets below. Every
fix is mutation-pinned: reverting it makes its tests fail.

### Fixed

- **Regression: `Action.fail_transition`'s in-flight probe was unguarded**
  (#194, introduced in 0.13.0). The side-effect that brings the engine to the
  failure path may have rollback-poisoned the connection (`ATOMIC_REQUESTS`,
  any caller's `atomic`), in which case the probe itself raised
  `TransactionManagementError` — replacing the original exception at the
  caller and silently skipping both failure hook bundles. A probe failure now
  logs, skips the `failed_state` write (unknown means don't write), and lets
  the original exception re-raise with both hook bundles running.

- **The transient typing is bounded by one shared liveness classification**
  (#195, `TransitionMessage.in_flight_liveness`). A stranded row — one
  nothing is driving — used to keep answering "retry shortly" forever:
  0.13.0's `TransitionTemporarilyUnavailable` told generic handlers to retry
  while hook-path logging sat demoted at WARNING. Liveness now reads the
  watchdog's own signals first: an attempt inside its declared
  `started_at + timeout_seconds` budget (plus slack) is LIVE however old
  `modified` is — a healthy 40-minute declared-budget attempt is never
  called stranded at minute 16. Otherwise a row whose newest activity is
  within `max(RETRY_MINUTES × (MAX_ERRORS + 1), 15)` minutes is live; past
  that it is stranded and raises plain `TransitionNotAllowed` (paging at
  ERROR), with likely causes in the message — unscheduled beats
  (`django_logic.W002`), a queue backlog or worker outage longer than the
  horizon, or a lost broker message. The classification covers **both entry
  points**: the sync gate and phase 1's constraint rejection (a stranded row
  no longer raises `AlreadyInProgress` on a background re-drive — the most
  likely consumer retry path). A row that completed in the race window keeps
  the transient answer: it just finished, so retrying is exactly right.

- **The sync `failed_state` writers get the #189 treatment** (#192). Both
  sync savepoints — `Transition.fail_transition` and the Action's
  write-under-lock — now pass `require_commit=True`, so a silently discarded
  write (the receiver-authored suppressed-database-error idiom at the one
  spot with no query after it) takes the honest except-branch instead of
  logging a false `SET_STATE`. The original exception still re-raises
  unchanged. The except-branches (all four terminal writers) also restore
  the in-memory state attribute the discarded savepoint left refreshed, so
  failure hooks and the sync caller never observe a state the database
  never had.

- **The `LEGACY_EXCEPTION_BASE` smoke probe is airtight** (#196). It now
  verifies the bridged class preserves the denial message — `args` must
  survive exactly and the message must appear in `str()` (a fork `__str__`
  that *formats* the preserved message is accepted; a message-eating
  `__init__` that used to boot green and blank every denial, breaking
  pickling too, is rejected) — and the `__bases__` unwind runs on
  `BaseException`, so a fork `__init__` raising `SystemExit`/
  `KeyboardInterrupt` during boot cannot leave the class half-mutated.

### Added

- **`django_logic.background.in_flight(instance, process_name='process')`**
  (#197) — a documented probe for shaping "busy, try again shortly" answers
  at consumer API seams without poking engine internals or duplicating the
  marker filter. It answers the *busy* question: `True` only for a LIVE
  uncompleted row (same classification as the gates), `False` for a
  stranded one — so a consumer answering 409 on this probe and 400 on plain
  `TransitionNotAllowed` stays consistent with what the engine raises. Racy
  by nature (the engine's own guards stay authoritative); `False` when the
  background app is not installed, without touching the database. The
  engine's failure-path write-skip deliberately stays bare existence — an
  Action must never clobber an uncompleted row's instance, stranded or not.
  The marker filter itself now lives in exactly one place
  (`TransitionMessage.in_flight_for`), so the #184/#186 identity rework will
  change it once.

## [0.13.0] — 2026-08-04

A small, deliberate release triaged from what the 0.12.0 review passes filed
(#185, #187, #189) and what a consumer's August release review raised (#190,
#191). Two additions make coexistence and generic error handling first-class
for consumers; two fixes close honesty gaps on the failure paths; and the
0.12.0 multi-database routing fix is finally pinned by tests. An adversarial
review pass over the release's own diff then confirmed five gaps in the first
cut — each fixed and mutation-pinned before shipping (they are folded into
the bullets below). Deliberately **not** here: the lock-key identity rework
(#184, #186) — changing the key's shape breaks lock recognition between old
and new processes during a rolling deploy, so it gets a dedicated release
with a migration note rather than a quiet ride in a mixed minor.

### Added

- **`TransitionTemporarilyUnavailable`** (#191, `django_logic.exceptions`) — a
  shared base for the transient concurrency refusals, slotted between
  `AlreadyInProgress`/`SourceStateChanged` and `TransitionNotAllowed`. "Busy,
  retry shortly" and "you may not do this" were indistinguishable at a generic
  top-level `except TransitionNotAllowed`, so whatever answer that handler gave
  was wrong for one of them — typically telling a user who clicked during an
  in-flight background transition that the action *is not allowed*. One
  `except TransitionTemporarilyUnavailable` ahead of the base now answers
  409/retry, without importing the background subpackage or enumerating
  subclasses per call site. The sync gate that rejects a transition while an
  uncompleted `TransitionMessage` exists raises it too — that was the
  motivating scenario, and it resolves exactly when the flight completes.
  Existing catches keep working (the base still
  subclasses `TransitionNotAllowed`); the hook runner's WARNING-vs-ERROR
  distinction (#154) now branches on this type. Lock contention ("State is
  locked") deliberately stays plain `TransitionNotAllowed` — a TTL-stuck lock
  is not "retry shortly".

- **`DJANGO_LOGIC['LEGACY_EXCEPTION_BASE']`** (#190) — first-class coexistence
  with a differently-named fork during a migration. Declare the fork's
  `TransitionNotAllowed` by dotted path and it is mixed into this engine's
  `TransitionNotAllowed.__bases__` at `AppConfig.ready()`, so shared handlers
  that catch the fork's class keep answering gracefully while apps migrate one
  at a time. The only prior option was a local patch to `exceptions.py` — a
  patch that re-vendoring has now silently destroyed twice, each time turning
  graceful refusals into HTTP 500s with no test failing. Zero cost when unset;
  every failure mode (unimportable path, non-exception class, MRO conflict)
  raises `ImproperlyConfigured` at boot, because a broken bridge must never be
  silent — including a fork class whose non-message `__init__` would have
  serviced every denial's construction through the new MRO: the installer
  smoke-constructs the bridged class and unwinds the mutation if that fails.

### Fixed

- **`Action.fail_transition` could clobber an in-flight transition's state**
  (#185). The `failed_state` write was guarded by `is_locked()` and then
  performed non-atomically, so a `Transition` (or a background phase 1)
  acquiring the lock in the window between check and write had its state
  overwritten by the Action's stale write — against a background flight, the
  phase-2 state guard would then supersede the whole flight. The write now
  happens only under an atomically-acquired lock (cache `add`) — and, because
  the cache lock is free for a background flight's entire queued/phase-2 span,
  the durable marker is consulted under it too: while an uncompleted
  `TransitionMessage` exists the write is skipped and logged, since phase 2
  owns the state field until the row completes. The lock releases through the
  shared path, so it emits the `Lock`/`Unlock` lifecycle lines (#188) and
  honours `DEFER_UNLOCK_UNTIL_COMMIT` when the write landed (#141). Actions
  still run their side-effects lock-free; the acquire is scoped to the one
  `UPDATE`.

- **A silently-discarded `failed_state` write logged a false `SET_STATE`**
  (#189, completing the `require_commit` fix from 0.12.0's `bd1445a`). The
  terminal failure paths — `_handle_failure` and the watchdog/detect-stuck
  finalizer — wrote `failed_state` through `_run_in_savepoint` without
  `require_commit`, so a consumer receiver that raises a database error and
  suppresses it (the receiver-authored `try: save() except IntegrityError:
  pass` idiom, no nested `atomic()`) made the savepoint roll back silently
  while the trace said the write landed. Both sites now pass
  `require_commit=True`, routing the rollback into the existing honest
  except-branch: log the failure, record it on the row where an operator will
  see it, complete the row anyway. The `failure_side_effects` bundle was the
  third terminal savepoint with the same hole — a silently discarded cleanup
  bundle is now returned and recorded on the row instead of vanishing behind
  success-shaped bookkeeping.

### Tests

- **The 0.12.0 multi-database alias routing is now pinned** (#187). A second
  in-memory SQLite alias in the test settings plus routed fixtures assert that
  `State.get_persisted_state()` reads the instance's own database and that the
  engine's savepoints open on the instance's alias — the sync failure path,
  the background attempt, and *both* terminal `failed_state` writers
  (`_handle_failure` and the watchdog finalizer, the latter via a veto-shaped
  test, because only a rolled-back write can tell the aliases apart).
  Previously mutation testing showed all of it could be reverted to
  `default`-only with the whole suite staying green.

## [0.12.0] — 2026-07-30

A correctness release that ends in a design decision. Three acts:

**One — five engine defects** (#178–#182), found reviewing 0.11.0, three of
them capable of losing or corrupting work; every one reproduced before the fix
and pinned in `tests/test_issue_fixes_0_12.py`.

**Two — a fourth review pass over those fixes**: seven independent readings of
the library plus mutation testing of the new tests. It found two more serious
defects (a watchdog that could terminalise a healthy attempt, and a lock leak
the release's own change made reachable), an undeclared breaking change, two
assertions that proved nothing, and nine fixes with no pinning test at all —
all fixed and pinned in `tests/test_pass4_*.py`, every pin verified by
reverting its fix and confirming the test fails. Plus #188, from a live
production incident: a failed lock acquisition is finally visible in the logs.

**Three — the headline: `in_progress_state` is background-only, and the
stranded sweep is retired** (see *Removed (breaking)*). Four review passes kept
finding their most serious defects in the same place — the machinery that
recovers instances from a marker the engine itself wrote with no durable
record. Rather than harden that machinery a fifth time, this release removes
the reason it exists. The engine is smaller, the beat schedule is four tasks
instead of five, and a killed synchronous run now rolls back to its source
state and is simply re-drivable.

Validated twice on real infrastructure (broker, PostgreSQL, workers, beat) via
the `django-logic-test` rig — once after act two, once after act three: 31
matrix rows each, zero failures.

### Changed (breaking)

Three definitions the engine used to accept now raise, because it could never
honour them. Each is a defect in the *declaration*, so a project hitting one was
already broken — but it will now fail at import or bind time rather than
silently doing nothing:

- an `action_name` shadowed by a `Process` attribute *or* by one of the
  attributes `Process.__init__` sets (`state`, `instance`, `field_name`) —
  `ImproperlyConfigured` at class creation;
- a `process_name` that already names something on the model's MRO (a field's
  descriptor, a method, a property, a manager) — `ImproperlyConfigured` at
  `bind_model_process`. Reverse FK/M2M *query* names are deliberately not
  flagged: they own no class attribute, so binding under one is sound;
- `sources` passed as a bare string — `ImproperlyConfigured`, since
  `list('draft')` silently became `['d','r','a','f','t']`.

`STRICT_KWARGS_SERIALIZATION` and `STRICT_HOOK_SIGNATURES` now require a real
bool and are validated at boot; a project passing the *string* `'false'` had
strict mode silently ON and will now get `ImproperlyConfigured` naming the key.

`django_logic.W004` is a new warning, so a project that keeps unrelated keys in
`DJANGO_LOGIC` and runs `manage.py check --fail-level WARNING` will need
`SILENCED_SYSTEM_CHECKS`.

### Removed (breaking) — `in_progress_state` is background-only; the stranded sweep and E001 are retired

The single largest simplification in the library's history, and a deliberate
design decision rather than a bug fix: **the in-progress marker now exists only
where it has a durable recovery owner.**

- **`in_progress_state` on a synchronous `Transition`/`Action` raises
  `ImproperlyConfigured` at class creation.** On a `BackgroundTransition` the
  marker is written atomically with the `TransitionMessage` row, so every
  marked instance carries its exact transition and the TM safety nets own its
  whole flight. A synchronous transition wrote the marker under a cache lock
  with *no durable record*: a hard-killed worker left the instance parked in a
  state with no outbound edges and nothing that could ever move it (#136) —
  the incident shape that produced the stranded sweep, and, across four review
  passes, the sweep and its ownership rules were the most defect-dense
  subsystem in the engine. Without the marker, a killed synchronous run rolls
  back to its source state and is simply re-drivable once the lock TTL
  expires: self-healing, no machinery.

- **`recover_stranded_states` is retired** (the fifth safety-net task, its
  beat entry, and the whole sweeping subsystem — candidate scans, lock-guarded
  recovery, ownership transfer, ambiguity skips). Record-less stranding is now
  structurally impossible: sync transitions cannot write a marker, a
  background marker always has a TM row, and a crash inside phase 1 rolls both
  back together. `beat_schedule()` ships **four** entries; a schedule still
  naming `django_logic.recover_stranded_states` simply has one stale entry
  (django_logic.W002 matches by task name and will not complain about extras).

- **`django_logic.E001` is retired.** The shared-`in_progress_state`
  ownership check existed to scope the sweep — a record-less stranded instance
  had no provenance, so transitions sharing a marker had to agree on recovery.
  With recovery TM-scoped, sharing a marker is harmless, in one process or
  across many. Projects silencing or asserting on E001 can drop it.

- **Consequences an operator will notice.** Rows the safety nets complete
  *without* managing a `failed_state` write (an unrestorable row, a rejected
  `failed_state` write) now leave the instance parked in its
  `in_progress_state` — visibly, with the reason on the completed row — and
  re-drivable via the implicit source, instead of being force-failed by the
  sweep on a later tick. Monitor `last_error_message`/
  `failure_side_effect_error` for the `[unrestorable]` / `failed_state write:`
  markers.

- **Migrating a synchronous transition that declared the marker:** model the
  busy phase as a real state — a fast transition into it, chained via
  `next_transition` to a `BackgroundTransition` that does the work (readers
  see the busy state exactly as before, and the work becomes TM-durable). The
  pattern accepts one narrow window the atomic marker did not have: a crash
  between the first transition's commit and the chained dispatch parks the
  instance in the busy state with no row. The README documents the three-line
  periodic re-drive that covers it — safe by construction, because in-flight
  instances raise `AlreadyInProgress`, and it retries *forward* instead of
  force-failing. Instances already stranded in a marker state by a PREVIOUS
  release are not recovered by anything after the upgrade: re-drive or
  hand-fix them BEFORE upgrading (the sweep in 0.9.1–0.11.0 can do it for
  you), or clean them up with your own command afterwards.

Two more declarations now raise, found in the fourth pass:

- `failed_state` equal to `in_progress_state` — `ImproperlyConfigured` at class
  creation. The two states are what recovery reads to tell "failed" from "still
  running"; identical, `recover_stranded_states` wrote `failed_state`, re-read
  the same `in_progress_state`, concluded the recovery had not landed, and
  re-ran the failure hooks on every sweep tick forever;
- a kwarg named `state`, `exception` or `deferrable` passed to a transition —
  `TypeError` before the lock is taken. These name the engine's own parameters
  on the state-change path, so they used to raise deep on the *failure* path
  instead: `failed_state` was never applied, the real exception was replaced by
  the `TypeError`, and the state lock leaked until its TTL (hours). Nest the
  value or rename it. Distinct from the lineage names (`tr_id`, `root_id`, …),
  which the engine forwards itself and therefore still cannot refuse.

**Background kwargs containing a NUL byte or a lone surrogate now raise
`KwargsSerializationError` at phase 1**, on every backend and regardless of
`STRICT_KWARGS_SERIALIZATION`. This shipped unannounced in the original 0.12.0
branch and is called out here because it is a real behaviour change: SQLite
stored such values happily, and PostgreSQL failed later, inside the worker,
where the failure was much harder to attribute. Failing at the call site is the
same trade the non-finite-float rejection made in 0.7.0.

**`State.get_persisted_state()` now reads the instance's own database alias**
rather than always `default`. On a multi-database project with routing, the
under-lock revalidation and the phase-2 state guard previously read the wrong
database. No effect on single-database projects.

**Two `TransitionMessage` text columns changed shape.**
`failure_side_effect_error` accumulates labelled entries (`failed_state write:
…; failure_side_effects: …`) instead of being overwritten, and
`last_error_message` has NUL bytes escaped (`\x00`) and lone surrogates
replaced. Anything parsing these columns should expect the new shapes.

**Coverage keys changed shape for `functools.partial` and callable-instance
conditions.** The per-declaration key includes a fingerprint of each condition;
0.9.1–0.11.0 rendered every `partial` as the literal `partial` and a callable
instance as its bare class name, so the per-variant declarations the fingerprint
exists to separate collapsed into one key and could report false coverage — the
per-courier `Condition('ups')` / `Condition('dhl')` pattern being the case that
matters. They now render as `partial(<func>(<bound args>))` and the
module-qualified class name plus the instance's configuration. Consequence: a
`TRANSITION_COVERAGE_LOG` recorded by an earlier release no longer matches such
declarations — they read as uncovered. Record a fresh log per run, which is the
documented practice. Named-function conditions keep their keys.

### Fixed

- **A failing state write no longer escapes phase-2 error accounting**
  (#178). The side-effect loop was savepointed so "the error bookkeeping below
  always works", but both `set_state` calls sat outside any savepoint. A write
  the database rejected — CHECK constraint, `pre_save` receiver, `save()`
  override, column length — escaped the outer atomic and **took `record_error`
  with it**: `errors_count` stayed 0, so `retry_stale_transitions`
  re-dispatched the row on every tick and its side-effects, including
  non-idempotent external calls, re-ran forever. `detect_stuck_transitions`
  never saw it (errors below `MAX_ERRORS`), `recover_stranded_states` skipped
  it (an uncompleted row existed), and the instance sat in its
  `in_progress_state` permanently. The terminal branch was worse: it rolled
  back the `record_error` immediately preceding it, pinning `errors_count` one
  below `MAX_ERRORS` for good — and the safety-net finalizer had the identical
  unguarded write, so nothing could rescue the row.

  The target write now happens **inside** the attempt savepoint, which also
  restores the documented all-or-nothing-per-attempt contract. Both
  `failed_state` writes are savepointed and never allowed to escape: completing
  the row is what stops the retry loop, so a rejected `failed_state` is logged
  and recorded on the row rather than preventing completion, leaving the
  instance for stranded recovery instead of an infinite loop.

- **The timeout watchdog works, and charges an attempt at most once** (#179).
  `mark_as_started` ran inside the attempt's `atomic`, so `started_at` was
  invisible to other connections *while* the attempt ran and was rolled back
  when a worker died — the watchdog could never see the attempts it exists to
  abandon. The only rows it matched were ones whose attempt had already
  committed a failure, and it re-charged them on every tick: one real attempt
  plus three ticks took `errors_count` from 1 to 4 with no new work, and a
  fourth terminalised the row, discarding every remaining retry. It also
  overwrote the real error message with a synthetic timeout. **Declaring
  `timeout=` made a transition strictly less reliable than omitting it.**

  `started_at` is now written in its own committed statement before the attempt
  and deliberately survives the attempt rolling back, so a hung or crashed
  attempt is observable; and the watchdog skips any attempt that has recorded
  an error since it started. `started_at` is therefore set on paths that exit
  early (superseded, unrestorable) — `duration_ms` remains the field that
  distinguishes "no work was measured".

- **A nested `Process` reachable by two paths is callable again** (#180).
  `_iter_available_with_owner` was the only tree walker without a visited set,
  so a diamond — or the same class listed twice in `nested_processes` — yielded
  each of its transitions twice and resolution rejected the single declaration
  as "several transitions available", with a hint no condition could satisfy.
  `get_available_actions()` set-dedupes, so the action was advertised and then
  failed on every call. A nested cycle recursed to `RecursionError`. The walk
  now visits each Process class once, which preserves the supported pattern of
  one `action_name` on distinct nested classes disambiguated by conditions.

- **Rendering `{{ obj.process.action }}` no longer executes the transition**
  (#181). Django calls any callable a template resolves, so referencing a
  transition instead of listing `get_available_actions` drove the state machine
  during rendering and printed the `tr_id` into the page. The dispatcher now
  carries `alters_data`, the marker `Model.save`/`delete` use; templates render
  `''` instead.

- **Four definitions the engine accepted but could not honour are now
  rejected** (#182):
  - An `action_name` shadowed by a real `Process` attribute (`state`,
    `is_valid`, `transitions`…) was advertised by `get_available_actions()` and
    silently did nothing, because `__getattr__` only runs when attribute lookup
    *fails*. Rejected at class creation.
  - A `process_name` colliding with one of the model's own fields replaced that
    field's descriptor with a read-only property, after which the model could
    not be instantiated at all. Rejected at bind time.
  - `STRICT_KWARGS_SERIALIZATION` was `bool()`-coerced, so the string
    `'false'` — an environment variable read straight through — switched strict
    mode **on**. Only a literal `True` enables it now, validated at boot;
    `STRICT_HOOK_SIGNATURES` gets the same treatment.

- `ProcessScenario.process_name` defaults to `process_class.process_name`
  instead of the literal `'process'`. A scenario for a process bound under a
  custom name no longer has to repeat it, and forgetting no longer surfaced as
  a bare `AttributeError` from inside an assertion. `assert_changed` explains
  the `{field: (old, new)}` shape instead of failing with "too many values to
  unpack".

### Added

- **`django_logic.W004`** reports `DJANGO_LOGIC` keys the engine never reads
  (#182). The settings dict has no schema, so a typo — `LOCK_TIMOUT`,
  `TRANSITION_MESSAGE_MAX_ERROR` — was silently ignored and the default
  silently applied. Removed keys are left to `W003`, which already names them
  with migration advice.

### Removed

- **The `[drf]` extra.** It installed `djangorestframework` and nothing in the
  library has referenced `rest_framework` since 0.4. A DRF integration module
  is still on the roadmap; it can bring its own extra back when it exists.
- **The nested-process tree walk existed five times.** Four class-level
  re-implementations — in `checks.py`, `coverage.py`, `testing/runner.py` and
  `collect_ambiguous_in_progress_states` — now call the canonical
  `_iter_process_tree`. This was not cosmetic: the fifth copy, the one on the
  runtime path, was the only one without a visited set, which is #180. The two
  remaining walks build *instances* (each sub-process sharing the parent's
  state), so they are genuinely different and stay.
- A duplicate failure-callback runner (two functions, byte-identical bodies and
  log lines, differing only in argument shape), `_validated_number`'s
  `allow_zero` parameter (never passed, its branch unreachable), and a
  defensive branch in `_recover_stranded_instance` whose own comment said it
  could not be reached.
- `ProcessScenario`'s `expect_raises=` no longer re-implements the three
  caller-boundary predicates; it routes through `assert_raised` /
  `assert_not_raised`, so the contract is pinned in one place instead of two.

### Fixed (from the same review — lower severity)

- **`sources` passed as a bare string is rejected.** `sources='draft'` became
  `['d','r','a','f','t']`, matching no state: the transition was invisible to
  `get_available_actions()` and calling it reported a missing action rather
  than a bad declaration.
- **A rejected `failed_state` write no longer masks the original failure** on
  the synchronous path. `fail_transition`'s docstring promised "the original
  side-effect exception keeps propagating either way"; the write's own
  exception used to win, losing the real cause. (The background equivalents are
  #178.)
- **The `DEFER_UNLOCK_UNTIL_COMMIT` registry clears again after a rollback.**
  It registered its `on_commit` clear only while empty; a rollback discards the
  hook but leaves the entries, so the registry was never empty again, no
  further clear was ever registered, and the list grew for the life of the
  connection — pinning every `State` it held.
- **A caller-supplied `user_id` kwarg is dropped loudly** instead of being
  silently consumed. `user_id` is the engine's wire form for `user`, so phase 2
  replaced the caller's value with a live user object and the hook never saw
  it — while the identical call behaved correctly in sync mode, a parity break
  that only appeared in production.
- `django_logic/__init__.py` defines `__all__`, so `from django_logic import *`
  no longer leaks whichever submodules happened to be imported (the namespace
  varied with `INSTALLED_APPS`). The six command bundles are now all swappable:
  `failure_side_effects_class` and `failure_callbacks_class` join the four that
  already existed, which had left `FailureSideEffects` a top-level export with
  no way to substitute it.
- `ProcessScenario.process_name` derives from `process_class.process_name`, and
  `assert_changed` explains the `{field: (old, new)}` shape rather than failing
  with "too many values to unpack".

### Fixed (fourth review pass — reviewing the fixes above)

- **The timeout watchdog could charge, and terminalise, a healthy attempt.**
  `watchdog_stale_attempts` decided "this attempt is stale" from its
  unsynchronised candidate scan and never re-verified under the row lock. A
  retry that stamped a fresh `started_at` in the scan→lock window defeated the
  one-charge guard added above — that guard compares the last error to
  `started_at`, and the new stamp is *later* than the old error — so an attempt
  milliseconds old was charged a synthetic `TimeoutError`, and at `MAX_ERRORS`
  the row was finalized to `failed_state` while its worker was still running.
  The staleness check now runs on the locked read; the scan is only a hint.
  (`beat_schedule()` co-schedules the starter every 60s and the watchdog every
  120s on one queue, so the two hitting the same row in the same second is
  systematic, not exotic.)

- **A rolled-back phase-2 attempt leaked the state lock of any instance its
  side-effects had driven.** Under `DEFER_UNLOCK_UNTIL_COMMIT`, a side-effect
  that synchronously drives a transition on another instance — the fan-out and
  report-back recipes both encourage this — registers that instance's unlock on
  `transaction.on_commit` inside the attempt savepoint. When a later side-effect
  failed, Django discarded the hook with the savepoint while the outer
  transaction still committed the bookkeeping: the other instance's state write
  rolled back but its lock was held until `LOCK_TIMEOUT` (7200s by default),
  every transition on it raised `TransitionNotAllowed("State is locked")`, and
  the driver's own retries then burned `MAX_ERRORS` against that held lock. Every
  hook bundle already ran through the machinery built for exactly this
  (`_run_in_savepoint`); the attempt savepoint was the one raw `atomic` left.
  Moving #178's target write inside that savepoint added a new way to reach it.

- **The safety-net finalizers now honour a manual fix whole, not just its state
  write.** On a state-guard mismatch, `detect_stuck_transitions` and the
  watchdog skipped the `failed_state` write but still ran `failure_side_effects`
  *and* `failure_callbacks` against an instance an operator had already
  resolved — destructive cleanup (refunds, releases) and report-back callbacks
  for a child that was fixed by hand — and completed the row with no marker
  explaining why. They now complete it as `[superseded]` with no hooks, matching
  what phase 2 has done since 0.10.0, and preserve the original error text after
  the marker.

- **A restore failure the engine had not classified escaped phase 2 unaccounted.**
  `_restore` treats model-uninstalled / row-gone / transition-renamed as
  permanent and stops retrying. Anything else — a consumer `process` property
  raising, an `instance_id` that no longer coerces to the pk type after a
  migration, a transient database error — propagated with `errors_count` still
  0, so the starter re-dispatched the row forever: the same unaccounted
  infinite-retry class #178 closed for state writes. Such failures are now
  charged like any attempt failure, so transient causes get their retries and
  permanent ones reach `MAX_ERRORS` and stop. The same escape inside the
  safety-net finalizer rolled back the whole finalization on every tick.

- **The stop-retry write could itself be the statement that failed.**
  `_mark_unrestorable_completed` was the one `last_error_message` writer still
  bypassing `db_safe_text`, and its text embeds an arbitrary import error — a
  NUL byte in it made the UPDATE fail on PostgreSQL, so the row never completed
  and was re-dispatched forever, defeating the guarantee the function exists to
  provide.

- **Two `nested_processes` walks in the phase-2 restore path had no cycle
  guard.** #180 made the sync walk cycle-safe and its test blesses A→B→A, but
  `_find_background_transition_in_owner` and `_background_transitions_named`
  still recursed until `RecursionError` on such a topology — on the blank or
  stale `owning_process_class` fall-throughs their caller is written to handle
  gracefully. Both now walk through `_iter_process_tree`.

- **An exotic-but-legal transition could take down `manage.py check` and the
  stranded sweep.** The E001 collector read `failed_state` and the failure
  bundles unguarded, so a duck-typed custom transition that declares
  `in_progress_state` and none of them raised `AttributeError` — in every
  `manage.py check`, and on every beat tick of `recover_stranded_states`, where
  the call sits before the per-binding containment and so killed the sweep for
  the whole deployment. Guarded like the hook-signature collector already was.

- **Background transitions bound with `django_logic.background` missing from
  `INSTALLED_APPS` are now reported** (`django_logic.E003`). Every existing
  check gated on the app being installed, so this misconfiguration passed
  `check` silently and surfaced as a raw `OperationalError: no such table` on
  the first drive.

- **`DJANGO_LOGIC` set to a non-dict** now raises `ImproperlyConfigured` naming
  the setting instead of an `AttributeError` from inside `_conf()`, and
  **`TRANSITION_COVERAGE_LOG` is type-validated at boot** — `open()` accepts a
  bool as a file descriptor, so `True` appended coverage lines to stdout.

- **The shadowed-`action_name` check no longer rejects working topologies.** It
  flagged a name shadowed by an attribute on *any* class in the nested tree, but
  dispatch only enters through the bound root's `__getattr__`, so a helper
  stored on a sibling nested Process — a natural pattern — failed at import with
  an error message that was demonstrably false. Root-only loses nothing: every
  `Process` subclass is validated as its own root when defined.

- **MTI and proxy models sharing one inherited state column are no longer
  invisible to E001.** The ambiguity collector keyed on the bound model's label,
  so a parent and a child bound to *different* processes could claim the same
  `in_progress_state` on the same physical column with different recovery — the
  cross-process case E001 exists to refuse. It now keys on the concrete model
  that owns the column.

- **`ProcessManager.unbind_model_process()`** removes a binding and its model
  accessor — the inverse of `bind_model_process`, for consumer test teardown.
  The library's own tests had been hand-rolling it.

- **Testing-framework assertions no longer pass vacuously on a bare string.**
  `assert_not_available(order, 'approve')` iterated the string per character, so
  it tested single letters and passed while `approve` was in fact available —
  the same footgun `sources` now rejects. The name-collection helpers raise
  `TypeError`.

- **Snapshot round-trips are lossless and loud.** `snapshot()` omitted
  `started_at`, `last_error_dt`, `failure_side_effect_error`, `completed_at` and
  `duration_ms`, so a snapshot of the row a timeout incident produced could not
  reproduce it (the watchdog filters on `started_at`); and `from_snapshot()`
  ignored the recorded model label while silently dropping unknown fields, so a
  wrong-model snapshot restored corrupted instead of failing. It now raises, and
  purges stale `TransitionMessage` rows for the instance before inserting the
  snapshot's own (they do not FK-cascade, so an orphan could be replayed
  instead).

- **`ScenarioAssertions` no longer needs `process_name` spelled out** — it
  derives from `process_class`, where a `None` default used to become
  `process_name=None` and make assertions pass or fail for the wrong reason.
  Now pinned by a scenario that omits it.

- **`record_failure_side_effect_error` keeps the newest note.** Truncation kept
  the head, so once the accumulated text approached the 10k limit, the note just
  appended — the most recent diagnostic — was what got cut.

- **A failed lock acquisition is logged before the raise** (#188). A frozen
  instance (leaked lock) and a healthy start were indistinguishable: both
  emitted one `Start` line and nothing else, because `change_state` raised
  `TransitionNotAllowed("State is locked")` before logging anything. In the
  incident that surfaced this, seven instances re-driven every 20 minutes for
  ten days produced ~1400 `Start` lines and zero indication a lock was the
  cause — and the wrong conclusion ("the worker drops it") made it into a bug
  report. Both call sites now log `Lock failed <instance_key> — state is
  locked` at INFO (not ERROR — losing the lock race is expected concurrency,
  per #154) before raising; the background phase-1 conditions/permissions
  rejection gets the same treatment. The `Lock`/`Unlock` lifecycle lines now
  carry `instance_key`, so a per-instance log filter shows whether the lock
  was ever taken or released without a `tr_id` self-join — previously `Start`
  was the only line naming the instance, which made the *absence* of a `Lock`
  line invisible during triage. The revalidation-failure release also logs
  (`Unlock <instance_key> after revalidation failure`) so it cannot read as a
  leak. Log-format note for anything parsing these lines: `Lock` and `Unlock`
  gained a trailing `instance_key` argument.

- **An expected concurrency guard is no longer logged at ERROR** (#154). Phase
  1's post-create source recheck raises `SourceStateChanged` (a
  `TransitionNotAllowed` subclass, so existing `except` clauses are unaffected),
  and the hook runners log it and `AlreadyInProgress` at WARNING. Both mean
  "another flight owns this instance right now" — the guard working — and at
  ERROR they paged an on-call for healthy contention, which is the common shape
  when a background transition is driven from another transition's side-effects.

- Two of this release's own regression tests asserted nothing and now do: one
  filtered log output for `'Set state'` while the engine logs `'Set State'`, so
  its empty-list assertion passed however false the log was; the other claimed
  to prove the attempt savepoint rolls back but gave the transition no
  observable write to roll back. Both were caught by mutation testing, along
  with nine fixes that had no pinning test anywhere — including #178's terminal
  path, the headline of this release.

### Fixed (fifth review pass — reviewing the branch for release)

- **A phase-2 attempt whose writes were silently discarded was reported as a
  success.** A side-effect that raises a database error and suppresses it —
  `try: obj.save() except IntegrityError: pass` without the nested `atomic()`
  that idiom needs — leaves Django's `needs_rollback` set, so `Atomic.__exit__`
  discards every write in the attempt savepoint with *no exception
  propagating*. `_run_in_savepoint` already detected that case (it releases the
  deferred unlocks the rollback dropped) but returned normally, and phase 2
  reads "returned" as "committed": the row was marked completed with
  `errors_count=0` and the success callbacks and `next_transition` ran on top of
  work that no longer existed. It is now accounted as the failure it is, so the
  attempt retries and terminalises to `failed_state` like any other. Reachable
  on a `BackgroundAction`, which writes no state — a `BackgroundTransition` is
  protected by accident, because its target `set_state` is the last statement in
  the attempt and raises `TransactionManagementError` on the poisoned
  connection. **Pre-existing, not new in 0.12.0** (0.11.0 behaves identically
  for an action, and *worse* for a transition: it advanced the instance to
  `target` and completed the row). Correctly-written consumer code — the
  suppression wrapped in its own `atomic()` — is unaffected, and is pinned as
  such. A silent rollback now also logs a WARNING wherever it happens,
  including in the best-effort hook bundles that tolerate it, since otherwise
  the missing writes are the only trace. (Reported by Cursor Bugbot; its
  conclusion for the target-writing case did not reproduce.)

### Documentation

- Four statements retired by this release's own design cut, still shipping in
  the release: the deployment section promised `beat_schedule()` "routes all
  **five** tasks … stranded 300s" two lines under a heading that correctly says
  four (and named a `stranded_seconds=` keyword that no longer exists, so
  copying it raises `TypeError`); `BackgroundTransition`'s docstring still made
  a shared `in_progress_state` conditional on matching failure hooks and pointed
  at the deleted `django_logic.E001`, which the README and `CLAUDE.md` correctly
  describe as retired; three operator-facing log lines sent whoever read them to
  `recover_stranded_states`, deleted in the same commit; and the
  `failed_state == in_progress_state` error still justified itself by
  "stranded recovery can never settle". The prose and comments were updated with
  the cut — the runtime strings were not.

- The **Complete Example is runnable as printed**. Its conditions called
  `instance.items.all()` and read `instance.shipping_address` on a model that
  declared neither, so copying it verbatim raised an uncaught `AttributeError`
  — a 500, since the example's own view only catches `TransitionNotAllowed` —
  and the Troubleshooting section's recommended `get_available_actions()`
  diagnostic raised the same thing. The example now declares `Product`,
  `OrderItem` and `shipping_address`, wires up the previously-unused
  `is_payment_verified`, and notes that conditions must be total.
- **"Custom State Classes" says how to install one.** It showed a `State`
  subclass but never mentioned `Process.state_class`, the attribute that makes
  a process use it, so the recipe had no effect as written.
- The watchdog and coverage sections match the code: `started_at` is described
  as what it now is, and the claim that 0.8/0.9.0 coverage logs are still read
  is corrected — 0.10.0 removed those readers.
- `docs/design/BACKGROUND_TRANSITION_ANALYSIS.md`, which `CLAUDE.md` tells
  engine-changers to read first, no longer marks `STARTER_QUEUE` "Required" or
  claims every `BackgroundTransition` must carry a queue.
- Documented three behaviours that were silently true: a synchronous `Action`
  ignores `next_transition` (it has no completion to chain from, while a
  `BackgroundAction` does run it); `context` is scoped to one execution and is
  rebuilt empty in phase 2, so it cannot carry caller data across the queue;
  and `tr_id` / `root_id` / `parent_id` / `process_class` /
  `owning_process_class` are reserved names the engine overwrites.
- Corrected four smaller claims: `State.unlock` leaves a successor's lock
  intact silently (it logs nothing), the testing guide's catalog is 18
  scenarios not 15, its "(opt-in) snapshot" on assertion failure was removed in
  0.10.0, and two headings that slugified to the same anchor made three
  in-document links land on the wrong section.
- Removed `docs/research/race-condition-issue` — an extensionless, unlinked
  13KB traceback referencing API deleted in 0.10.0.


## [0.11.0] — 2026-07-28

### Changed (breaking)

- **`django-redis` is no longer a core dependency** — it moves to the `[redis]`
  extra, which stops being an empty alias (#173). The engine has never imported
  `django_redis`; the state lock goes through Django's cache API
  (`State.lock` → `cache.add`), so what it requires is a *cross-process cache
  backend*, and Django has shipped
  `django.core.cache.backends.redis.RedisCache` since 4.0 (our floor is 4.2).

  **Migration:** if your settings name `django_redis.cache.RedisCache`, install
  it explicitly — `pip install django-logic[redis]`. The failure mode if you
  miss it is *not* at boot: `django.setup()` succeeds and
  `InvalidCacheBackendError` is raised at the first cache access. The
  celery-mode locmem/dummy guard is backend-agnostic and unchanged.

### Changed

- **`in_progress_state` no longer has to be unique within a process** (#175).
  Two transitions in one process tree sharing an `in_progress_state` used to
  raise `ImproperlyConfigured` at class-creation time. The rule now enforced is
  the narrower one the engine actually needs: transitions sharing an
  `in_progress_state` on a given (model, state_field) must **recover a
  record-less stranded instance identically** — same `failed_state`, same
  failure hooks — and must belong to the **same bound process**. Where they
  agree, sharing is free and `recover_stranded_states` picks any claimant;
  where they disagree, `django_logic.E001` fails `manage.py check` and the
  sweep skips the state (#143).

  The old justification — "the in-progress state alone identifies the
  transition that's mid-flight" — stopped being true when `owning_process_class`
  was added to `TransitionMessage` (migration 0007): phase-2 restore resolves
  `(owning process class, action_name)` off the row, guarded by
  `_validate_unique_background_action_names`, and never searches by state.

  This unblocks several actions on one model that all mean "busy" to a client,
  sharing one in-progress value and one `failed_state`.

  Three things to know:

  - **Sharing across two different bound processes stays ambiguous.** The
    sweep's in-flight check is scoped by `process_name`, so a sibling process's
    open `TransitionMessage` is invisible to it — an instance legitimately
    mid-flight there would look record-less and be force-failed into
    `failed_state`. The recovery signature includes the bound process name so
    this topology still reports `E001` and is still skipped.
  - **Failure hooks are compared by object identity**, not equality: claimants
    must reference the *same* callables. Hoist a shared `partial`/`lambda` to a
    module-level name, or `E001` will report transitions that behave identically
    as recovering differently.
  - **A divergent shared state within one tree was previously impossible** — it
    raised at import — and is now constructible, caught by `E001` at
    `manage.py check` time. Celery workers do not run system checks, so make
    sure your deploy pipeline does.

### Fixed

- **Late migration note for the 0.10.0 removal of `django_logic.conditions`.**
  0.10.0 removed `all_related_in` / `any_related_in` (#168) as "public API with
  no callers in any tree". That audit scanned only one consumer; the
  `django-logic-test` validation rig imported both, and would have failed at
  import on upgrade. Nothing changes in 0.11.0 — the module is still gone — but
  the migration the changelog never gave is: **copy the two factories into your
  own project.** They are queryset arithmetic with no engine coupling:

  ```python
  def all_related_in(relation, field, states):
      wanted = set(states)

      def condition(instance, **kwargs) -> bool:
          manager = getattr(instance, relation)
          total = manager.count()
          if total == 0:
              return False
          return manager.filter(**{f'{field}__in': wanted}).count() == total

      return condition


  def any_related_in(relation, field, states):
      wanted = set(states)

      def condition(instance, **kwargs) -> bool:
          return getattr(instance, relation).filter(
              **{f'{field}__in': wanted}).exists()

      return condition
  ```

## [0.10.0] — 2026-07-27

An overengineering sweep. ~1,900 lines removed across the engine, its tests and
its docs, four settings retired, and one addition: a system check for the
failure mode that motivated the audit. Every removal below was verified to have
no consumer — the two real consumers were searched under both import spellings
before anything was deleted.

### Removed (breaking)

- **`RedisState`** and **`State.get_db_state()`**. `RedisState` was 199 of
  `state.py`'s 326 lines and no consumer had ever set `state_class` to it;
  measured on a live deployment of 28 bound machines / 137 transitions, all 50
  process classes resolved to the base `State`. Use `get_persisted_state()` in
  place of `get_db_state()`. Two hardening issues turn out to have been spent on
  dead code — #139's token-in-key wrapper and #151's Lua compare-and-set — while
  the base-`State` ownership token and compare-and-delete unlock stay.
  Deliberately **no** `RedisState = State` alias: the two differ in locking
  semantics, so an alias would change behaviour silently where an `ImportError`
  says so plainly.
- **`LOG_KWARGS` / `LOG_KWARGS_REDACTOR`.** Scrub with a `logging.Filter` on the
  `django-logic.transition` logger instead. `redact_log_kwargs` keeps its
  shallow copy, which is a real fix, not a knob.
- **`PHASE2_STATE_GUARD = 'warn'`.** The phase-2 state guard now always
  enforces; a stale `'warn'` is ignored. Its only purpose was restoring pre-0.4
  behaviour.
- **`SENTRY_TRANSACTION_NAMING`.** Transactions are always named.
- **`PROCESS_CLASS_ALIASES`** — removed rather than fixed, because it never did
  what it documented: the map was applied to the recorded *bound* class but
  `_find_background_transition_in_owner` compared `owning_process_class`
  verbatim, so renaming a *nested* owning process still yielded
  `[unrestorable]` with a correct alias configured. **Drain in-flight rows
  before renaming a Process class** — already the documented rule for the
  sibling `action_name` refactor.
- **`django_logic.conditions`** (`all_related_in`, `any_related_in`). The one
  consumer that wanted this pattern hand-wrote it four times with different
  empty-set semantics. `docs/recipes/nested-processes.md` now shows the closures
  inline.
- **`Process.get_transition_by_action_name`** and the public `ignore_state=`
  parameter of `get_available_transitions`. Neither had a caller in the library,
  its suite, or any consumer. To resolve one action, iterate
  `get_available_transitions(user=..., action_name='x')` and take the single
  match — that is what the removed method did. **Gotcha:** a stale call will not
  raise `AttributeError` pointing at this removal, because `Process.__getattr__`
  treats any non-underscore miss as an *action name*. Attribute access still
  returns a callable, and calling it reports an argument problem with a
  now-nonexistent action:

  ```
  TypeError: get_transition_by_action_name() accepts keyword arguments only
             (got 1 positional). Pass user and other values
  ```

  Grep for the name when upgrading rather than relying on the traceback.
- **`ProcessScenario.snapshot_on_failure`** and `format_failure(snapshot=)`,
  never enabled by anyone in the 14 months since they shipped.
  `snapshot()` / `from_snapshot()` are unaffected.
- **`BaseTransition`** — folded into `Transition`, along with the
  `conditions_class` / `failure_callbacks_class` / `failure_side_effects_class` /
  `next_transition_class` swap hooks that had no override anywhere.
  `Transition.is_background`, `side_effects_class`, `callbacks_class`,
  `permissions_class`, `conditions_class` and `Process.permissions_class` all
  stay: consumers use them.
- **`ExecutionTracker`** is no longer re-exported from `django_logic.testing`
  (the class remains; `track()` is the supported path).
- **`process_name` is now required** on `latest_message`, `message_for` and
  `uncompleted_message`. The `None` default made it possible to silently
  reintroduce the #150 cross-process bleed by omitting an argument.
- **`JourneyStep.matches()`** — use `==`.
- **`coverage_report` no longer reads 0.8 or 0.9.0 log lines.** Record a fresh
  log per run, which was always the documented practice.
- **The non-finite-float pre-scan** in `serialize_kwargs`. The
  `json.dumps(allow_nan=False)` round-trip rejects a superset (it also catches a
  non-finite float in a dict *key*, which the scan could not reach); the error
  message no longer names the offending key path.
- **`_infer_field_name`**: a `TransitionMessage` with no `field_name` now fails
  closed as `[unrestorable]` instead of guessing `'state'`, which could drive
  the wrong machine on a multi-process model.

### Added

- **`django_logic.W002`** — under `BACKGROUND_EXECUTION='celery'`, reports any
  shipped safety-net task missing from the running app's beat schedule. This
  existed to catch a real incident: the README and `beat_schedule()`'s own
  docstring recommended `app.conf.beat_schedule = {...}`, which Celery silently
  ignores when the project also defines `CELERY_BEAT_SCHEDULE` in Django
  settings, and a consumer consequently ran seven weeks with all five tasks
  registered and never fired. Both now recommend
  `app.conf['CELERY_BEAT_SCHEDULE']`.
- **`django_logic.W003`** — reports any `DJANGO_LOGIC` key this release removed.
  `DJANGO_LOGIC` has no unknown-key rejection, so every removal above fails
  *open* and silently; the sharpest case is a deployment that set
  `LOG_KWARGS_REDACTOR` for PII compliance, upgrades, and starts writing raw
  kwargs to its logs with no signal anywhere. The warning names the replacement
  for each key.

### Fixed

- `TaskCrashRedeliveryConfigTests` hardcoded five task objects and so never
  covered `recover_stranded_states`; both `acks_late` and
  `reject_on_worker_lost` could be stripped from it with the suite green. The
  list is now derived from the module.
- The duplicated failure-side-effect savepoint in `background/runner.py`: #138
  had already moved that rule into `commands.FailureSideEffects.execute` with a
  stronger savepoint, and both call sites already ran inside the atomic block.

### Docs

- Deleted five stale pre-implementation documents and the index that tiered
  them (2,311 lines), whose rot was already realized — `docs/PLAN.md` omitted
  five shipped settings while `CLAUDE.md` told engine-changers to read it first.
  The two still-open items from the Heroku-validation notes moved to `TODO.md`.
- De-duplicated the prose that `README.md` and `CHANGELOG.md` already owned, and
  collapsed the README testing chapter's overlap with `docs/TESTING_GUIDE.md`.
- `CLAUDE.md` now states the comment/docstring rule that reviewers had been
  citing as though it existed.

### Repo

- Removed the nightly "stress" CI step (its `STABILITY_STRESS_MODE` was read by
  nothing, so it re-ran the identical tests), an unreferenced and broken
  `docker-compose.test.yml` service, and `.cursor/rules/` (no audience, five
  false statements). The `dev` extra dropped `pytest`/`pytest-django` — there is
  no pytest configuration, so collection could never have worked — and
  `djangorestframework`, which no test imports. The `drf` extra stays.
- Test settings: one `tests.dl_settings(**overrides)` helper replaces 26
  hand-copied dicts, and 13 no-op `@override_settings` are gone.


## [0.9.1] — 2026-07-24

### Fixed

- **`RedisState` lock refresh is atomic on django-redis** (#151). The
  value+lock key's refresh in `set_state` is now a single server-side
  compare-and-set (Lua): the new state is written only if the key still
  holds exactly the bytes the ownership decision was based on, so a
  takeover landing strictly between the read and the write can no longer
  re-plant a stale holder's token over a successor's lock — closing the
  residual window #139 documented. Off django-redis (the single-process
  test fake / LocMemCache) the plain read-write is unchanged; it cannot
  race there. The CAS runs on the raw django-redis connection, so a Redis
  connection error during the refresh now surfaces to the caller
  regardless of django-redis `IGNORE_EXCEPTIONS` (0.9.0 swallowed it on
  the cache-wrapped path and still committed the DB write) — the lock
  backend fails loud on an outage instead of proceeding without exclusion.

- **Savepoint unlock cleanup is exception-contained.** When a hook
  savepoint rollback releases the deferred unlocks it discarded (#141 ×
  #138), one release raising (a cache blip) no longer skips the sibling
  releases or replaces the hook's original exception — the missed
  release degrades to the documented TTL-bounded leak, logged.

- **Coverage keys carry a conditions fingerprint** (#146 follow-up).
  Same-class namesakes sharing sources→target and differing only by
  `conditions` — the per-courier polymorphism pattern — no longer
  collapse into one declaration: the key includes the condition
  callables' sorted qualnames. (Anonymous lambdas can still collide;
  named condition functions, the norm, stay distinct.) The wider key
  stays backward-compatible with persisted coverage logs: a 0.9.0-format
  line (no conditions field) covers every declaration sharing its
  `class⇥action⇥kind⇥sources>target` prefix, so a log carried across the
  upgrade does not spuriously read as all-uncovered (a fresh log per run,
  the documented practice, avoids cross-version keys entirely).

- **The missing-`failed_state` warn-once key includes
  `in_progress_state`** (#145 follow-up). Namesake transitions parking
  candidates in different states are different parked backlogs — the
  second is no longer silenced by the first's warning.

## [0.9.0] — 2026-07-23

Stranded-state recovery (#136): `recover_stranded_states`, the fifth
safety-net task, actively recovers instances that a hard-killed
*synchronous* transition left parked in an `in_progress_state`, driving
each through the owning transition's normal failure path. Per-transition
`lock_timeout` lets legitimately long side-effects declare their own lock
budget. Plus the #138–#150 hardening batch: lock ownership tokens, opt-in
`DEFER_UNLOCK_UNTIL_COMMIT`, per-hook savepoints, fail-closed restore of
unimportable process classes, binding validation, the `E001`/`E002` system
checks, and boot-time validation of every safety setting.

### Changed (breaking)

- **Transition observers receive the resolved declaration** (#146).
  `django_logic.process.transition_observers` callables are now invoked
  as `observer(owning_process_cls, action_name, instance, transition)` —
  the resolved transition object as a fourth argument. Observers written
  against the 0.8 three-argument form must add the parameter. Both
  built-in recorders are updated; coverage is now keyed **per
  declaration** (class + action + sync/background + sources→target), so
  legal same-name transitions — condition-disambiguated variants and
  sync+background namesake pairs — count and cover separately instead of
  collapsing into one entry. Report entries expose `sources`/`target`.
  File logs written by 0.8 recorders (2-field lines) are still accepted
  with their original cover-all-namesakes semantics.

- **`RedisState` storage format**. The single value+lock key now wraps
  `{state, ownership token}` (see lock ownership below). Deploy web and
  workers together; keys written by 0.8 stay readable (they carry no
  token, so only a force-release can delete them until reacquired).

- **Binding validation** (#143). `ProcessManager.bind_model_process`
  raises `ImproperlyConfigured` when a `(model, process_name)` is
  already bound to a different process/field (previously the model
  property was silently overwritten while the registry kept both
  claims), and when `state_field` is not a concrete model field.
  Identical re-binds are now idempotent no-ops. Topologies that were
  silently broken now fail at startup.

### Added

- **Lock ownership tokens** (#139). Every lock acquisition stores a
  unique token; `unlock()` is a compare-and-delete. A holder that
  outlives its lock TTL can no longer delete the lock a successor
  acquired (T1 expiry → T2 takeover → T1 late unlock previously let T3
  enter alongside T2). Tokenless `State` objects keep the historical
  unconditional delete as a manual force-release path. `RedisState`
  writes preserve the stored token on both holder and non-holder
  (xx-refresh) paths, and a holder whose key now carries a successor's
  token skips the refresh entirely (it must not re-plant its own token
  over the successor's lock). Residual: the get→compare→set refresh is
  not multi-process atomic — a takeover strictly between the two calls
  can still misplace a token (TTL-bounded leak for tokenless writers, a
  narrow wrong-unlock window for holders); a fully atomic refresh is
  tracked as #151.

- **`DJANGO_LOGIC['DEFER_UNLOCK_UNTIL_COMMIT']`** (#141, default off).
  Inside an outer `transaction.atomic()` a synchronous transition's
  state write is invisible until commit while the cache lock is not
  transactional — releasing on completion (the historical behavior,
  still the default) opens a window where a second connection acquires
  the lock, reads the old committed state, and runs conflicting
  side-effects. With the flag on, success/failure unlocks ride
  `transaction.on_commit` so exclusion covers the whole invisible span.
  Trade-offs (documented in the README): rollback leaves the lock to
  expire via TTL; same-instance follow-ups inside the atomic block are
  skipped as locked. Two-connection PostgreSQL regression tests pin both
  modes.

- **`django_logic.E001` system check** (#143): an `in_progress_state`
  claimed by more than one bound machine on the same (model,
  state_field) has no provenance for a record-less stranding —
  `recover_stranded_states` skips such states at runtime with a loud
  error, and the check flags the topology itself.

- **Process-scoped `ProcessScenario` message helpers** (#150).
  `uncompleted_message` / `latest_message` / `message_for` accept an
  optional `process_name`; `ProcessScenario` threads its own process
  through retries, error assertions, owner assertions, failure output
  and snapshots — with two machines bound to one model, a scenario no
  longer inspects or reruns a sibling's `TransitionMessage`.

- **`DJANGO_LOGIC['PROCESS_CLASS_ALIASES']`** (#140). A dict of
  old-dotted-path → new-dotted-path applied when restoring a
  `TransitionMessage`'s recorded process class, so in-flight rows drain
  correctly across a process rename instead of failing closed.

- **`django_logic.E002` system check** (#148). The background engine
  uses unqualified managers and bare `transaction.atomic()` — both
  resolve to the `default` alias — so the atomic-outbox invariant (state
  write + `TransitionMessage` row in one transaction) cannot hold when a
  database router sends `TransitionMessage` or a background-bound model
  elsewhere. Such topologies are now rejected at check time instead of
  silently degrading row locking, atomicity, and the one-in-flight
  constraint. Supported topology: `TransitionMessage` and every
  background-bound model share the `default` alias.

- **Boot-time validation of every safety setting** (#149).
  `TRANSITION_MESSAGE_MAX_ERRORS` (int ≥ 1), `RETRY_MINUTES` (≥ 0),
  `CLEANUP_DAYS` (≥ 0), `LOCK_TIMEOUT` (> 0, finite),
  `DEFER_UNLOCK_UNTIL_COMMIT` (real bool), `PROCESS_CLASS_ALIASES`
  (dict[str, str]) and `LOG_KWARGS_REDACTOR` (importable dotted path)
  are validated in `validate_on_ready()` for all execution modes —
  misconfiguration raises `ImproperlyConfigured` naming the setting at
  boot instead of failing inside a periodic task (booleans and NaN were
  previously accepted; a negative `RETRY_MINUTES` hot-looped the
  starter; a negative `CLEANUP_DAYS` deleted every completed row).
  The core knobs (`LOCK_TIMEOUT`, `DEFER_UNLOCK_UNTIL_COMMIT`) live in
  the new `django_logic.conf` and are additionally validated from the
  core `DjangoLogicConfig.ready`, so **sync-only installs** (without the
  background app) fail fast too; the runtime reader for
  `DEFER_UNLOCK_UNTIL_COMMIT` is strict — only a literal `True` defers,
  so truthy garbage (`'false'`) can never flip lock-release semantics.

### Removed

- **`django_logic.background.serializers.make_json_safe`.** The legacy
  lossy coercion helper (UUID/datetime → strings, tuples → lists) had
  zero callers since the typed encoder replaced it in 0.5.0. Anyone
  importing it directly should use `serialize_kwargs` /
  `encode_value` — they preserve types.

- **The `consumer-gv.yml` workflow.** A public library's CI should not
  name or check out a private consumer — the dependency points the wrong
  way. Consumer-contract validation moves to the consumers' own CI
  (install this repo at the candidate ref and run their suites);
  RELEASING.md documents the release gate in consumer-neutral terms. The
  job had also never actually run: its access token was never configured,
  so it always skipped with a warning while advertising the consumer's
  internals.

### Fixed

- **Unrestorable recorded process classes fail closed** (#140). A
  `TransitionMessage` whose recorded `process_class` no longer imports
  used to fall back to whatever process was currently bound under the
  same `process_name` — executing side effects phase 1 never asked for
  after a rename/removal. Restore now completes the row as terminal and
  auditable (`last_error_message` starts with `[unrestorable]`, no side
  effects, no state write), and the previously-unguarded load in the
  attribute-miss branch no longer escapes as a raw `ImportError` that
  re-dispatched the row forever without counting errors.

- **The stranded sweep is bounded and quiet** (#145). Candidate scans
  page by pk-keyset instead of materializing every matching primary key
  (a no-broker misconfiguration can park thousands of rows in an
  in-progress state), and the no-`failed_state` warning fires once per
  transition per process lifetime — hoisted out of the per-instance path
  so such candidates are never locked or touched — instead of re-warning
  for every candidate on every 5-minute tick.

- **Best-effort hooks no longer poison the caller's transaction**
  (#138). A database error swallowed by callbacks (success or failure),
  failure side-effects, or a `next_transition` follow-up inside an open
  `transaction.atomic()` marked the connection rollback-only — every
  later ORM call raised `TransactionManagementError` and the
  transition's own state write could roll back with the caller. Each
  callback now runs in its own savepoint when (and only when) the caller
  is inside an open transaction, and one failed callback no longer
  prevents the rest of the list from running; failure side-effects get
  the same bundle-level savepoint contract phase 2 already applied.

- **Custom `State.lock()` compatibility** (#142). `state_class` is a
  public extension point; the engine now calls `state.lock()` with no
  argument when a transition declares no `lock_timeout`, so subclasses
  written against the pre-`lock_timeout` `lock(self)` signature keep
  working. Non-finite `lock_timeout` values (NaN/Infinity) are rejected
  at declaration.

- **Docs and metadata drift** (#144, #147). README installs from PyPI
  (not a legacy tag), the Django floor is 4.2 everywhere, and the
  pyproject dependency is `django>=4.2,!=5.0.*` matching classifiers, CI
  matrix, and the 0.5.0 changelog. `docs/INDEX.md` separates current
  guidance from historical planning material; `tests/test_metadata.py`
  pins classifiers ↔ CI ↔ dependency ↔ README consistency as a
  pre-release check.

### Added (from the #136 line of work)

- **Per-transition `lock_timeout`**. `Transition(..., lock_timeout=14400)`
  overrides the global `DJANGO_LOGIC['LOCK_TIMEOUT']` for that
  transition's synchronous execution. The state lock is the liveness
  signal `recover_stranded_states` relies on — a sync run that outlives
  its lock TTL becomes indistinguishable from a stranded one — so
  transitions whose side-effects legitimately run long (report
  generation, large exports) declare their own budget instead of
  inflating the global for everyone. `RedisState` remembers the TTL the
  lock was taken with, so its `set_state` refreshes keep the custom
  lifetime instead of silently shortening it mid-run. Validated at
  declaration time (`ImproperlyConfigured` on non-positive values).
  Background transitions don't need it: their phase-1 critical section
  is short and the uncompleted `TransitionMessage` row — which shields
  them from the stranded sweep regardless of lock expiry — is their
  in-flight marker.

- **`recover_stranded_states` — the fifth safety-net task** (#136). A
  hard-killed *synchronous* transition (worker OOM / SIGKILL / dyno
  eviction mid side-effect) leaves its instance parked in the
  transition's `in_progress_state`: the lock self-expires after
  `LOCK_TIMEOUT` and the implicit-source rule keeps it re-drivable, but
  nothing *acted* — no failure hooks, no alert. The new periodic task
  (`django_logic.recover_stranded_states`, added to `beat_schedule()`;
  also callable inline via
  `django_logic.background.dispatch.recover_stranded_states`) walks
  `ProcessManager.bindings`, finds instances that are in a declared
  `in_progress_state` with **no lock held** and **no uncompleted
  `TransitionMessage` for that process** — provably stranded — and drives
  each through the owning transition's normal failure path
  (`failed_state`, failure side-effects, failure callbacks) with a
  synthetic `[stranded]` error, so standard alerting and retry paths
  apply. The in-flight-message shield is scoped by `process_name` (same
  as `_ensure_no_background_in_flight` and the partial unique
  constraint), so a sibling process's background row on the same
  instance cannot delay recovery. Recovery runs **under the state lock**
  with the phase-2 state-guard contract: the sweep takes the lock via
  the bound process's declared `state_class` (a live execution holding
  it means "not stranded"; a `RedisState` keeps a truthful state value
  visible under the key), re-checks the in-flight message **first** and
  only then re-reads the persisted state — order matters, because
  phase-2 completion holds no state lock and commits its state write
  atomically with `is_completed`, so a completion landing between the
  guards is always observed — a re-drive or manual fix that won the race
  always wins — and transfers lock ownership to `fail_transition`
  (called with the full hook contract, including `context`), so it never
  clobbers live work, never double-unlocks, and never releases a lock it
  doesn't own. Scans are chunked and each transition's sweep is
  exception-contained, so one oversized backlog or misbehaving model
  cannot abort the rest of the sweep. Stranded
  instances whose transition declares no `failed_state` are logged loudly
  and left re-drivable. `Action`s are never candidates: an Action accepts
  `in_progress_state` only as an implicit source and never writes it, and
  its `fail_transition` holds no lock (it neither unlocks nor writes
  `failed_state` while the state is locked) — so the ownership-transfer
  contract applies only to state-writing transitions. The sweep queries
  through `_base_manager` (like `State.get_persisted_state`), so a
  filtered or renamed default manager cannot hide stranded rows.
  Background transitions are unaffected: their
  durable row is already recovered by the starter / watchdog / stuck
  finalizer.
  (Issue #136 was reported against the legacy `django-logic 0.1.6` +
  `django-logic-celery` line, where the equivalent gap is unrecoverable —
  bounded lock TTLs, implicit-source re-drive, `reject_on_worker_lost`
  redelivery and the safety-net tasks all already exist on 0.5+; this
  closes the last piece, active recovery for record-less sync strandings.)

## [0.8.0] — 2026-07-20

Transition-execution coverage (#132): initiation observers on the resolver
plus a coverage report that answers "which transitions did the test suite
never drive?" exactly. A new top-level `django_logic` AppConfig activates
system checks and coverage recording for sync-only installs (closing the
W001 registration gap noted on #126), and the consumer-contract workflow
got the #119 polish (PR-runs on workflow edits, no persisted token,
reliable release-gate watch).

### Added

- **Transition-execution coverage** (#132). The resolver now notifies
  `django_logic.process.transition_observers` with
  `(owning_process_cls, action_name, instance)` on every transition
  initiation (direct calls, `next_transition` follow-ups, background
  phase 1; phase-2 restore does not re-notify). A raising observer is
  logged and never breaks the transition. On top of it,
  `django_logic.coverage` records executed `(process, action)` pairs and
  diffs them against every transition declared by `ProcessManager.bindings`
  (nested processes included): `TransitionCoverage` (in-memory context
  manager), `DJANGO_LOGIC['TRANSITION_COVERAGE_LOG'] = path` (file-backed,
  fork/spawn-safe parallel test runs), and `coverage_report()`. Static
  test-tree analysis cannot see transitive or dynamically-dispatched
  drives; the engine can — this answers "which transitions did the suite
  never drive?" exactly. The log is append-only (fresh path per run);
  `coverage_report` treats a never-written log as all-uncovered.
- **Top-level `django_logic` AppConfig.** System checks (`django_logic.W001`)
  and coverage-log activation now bootstrap for sync-only consumers that
  install just `'django_logic'` — previously both required the optional
  `django_logic.background` app (gap noted on #126). Idempotent with the
  background app's own `ready()`.

### Changed

- **consumer-gv workflow polish** (#119, items 2–4): PR runs trigger on
  edits to the workflow file itself, the gv checkout no longer persists
  `GV_REPO_TOKEN` on disk while gv-controlled code executes, and
  RELEASING.md's release gate resolves the dispatched run id instead of
  the unreliable bare `gh run watch` (and documents that the gate is
  vacuous until the secret is configured).

## [0.7.0] — 2026-07-17

Robustness and testing: the sync→background chain fix (#129), phase-2
decode hardening (#117), loud NaN/Infinity rejection (#118), an
`assert_idempotent` testing helper (#106), and parity-matrix coverage of
hook ordering and `next_transition` chaining (#127).

### Added

- **`django_logic.testing.assert_idempotent(fn, instance, *, fields=None,
  capture=None, refresh_from_db=True, **kwargs)`** (#106). Applies the
  side-effect twice and asserts the second application changes nothing
  observable (named instance fields and/or a `capture(instance)` callable
  for off-instance effects). Background side-effects re-run from scratch
  on every retry, so idempotence is a contract worth pinning per hook.
- **Parity matrix extensions** (#127): hook ordering (side-effects before
  the target write, callbacks after; on terminal failure `failed_state`
  first, then `failure_side_effects`, then `failure_callbacks`) and
  `next_transition` chaining are now pinned as identical across
  `Transition` / `Action` / `BackgroundTransition` / `BackgroundAction`.
  (The `Transition` docstring previously listed the failure order
  backwards; fixed to match all implementation sites.)

### Changed

- **Non-finite floats are rejected at phase 1** (#118). `float('nan')` /
  `float('inf')` pass Python's `json.dumps` (non-standard tokens) but are
  not valid JSON, so they previously failed backend-dependently at the
  row write (opaque on PostgreSQL, silently stored on SQLite). They now
  raise at dispatch with the offending path named, surfacing as
  `ImproperlyConfigured` like any unserializable kwarg. Pass `None` or an
  explicit sentinel instead. `Decimal('NaN')` is unaffected.


### Fixed

- **NextTransition no longer forwards `request` into background
  follow-ups** (#129). Under `STRICT_KWARGS_SERIALIZATION` the follow-up's
  phase-1 failure is swallowed by the best-effort next-transition hand-off,
  silently killing sync→background chains; sync follow-ups keep receiving
  `request`.
- **Phase-2 decode failures can no longer wedge an instance** (#117). A
  malformed payload for a known kwargs type tag warns and passes the raw
  tagged value through (mirroring the unknown-tag path) instead of
  crashing phase 2; and any residual `deserialize_kwargs` failure (e.g. a
  corrupt `user_id`) is accounted like an attempt failure — `errors_count`
  increments, retries honor `TRANSITION_MESSAGE_MAX_ERRORS`, and
  exhaustion routes `failed_state` — instead of escaping before the error
  bookkeeping and being re-dispatched forever. Watchdog terminal
  finalization proceeds with empty kwargs when a row no longer decodes.
- **Documented isoformat fidelity limits** (#118): a `ZoneInfo` tzinfo
  degrades to a fixed-offset `timezone` across the round-trip (UTC
  instant preserved, zone identity not) and `datetime.fold` is not
  preserved. `docs/PLAN.md` no longer describes the pre-0.5.0 lossy
  encoding (#120).

## [0.6.0] — 2026-07-17

Consumer-facing observability: a first-class bindings registry and a
Django system check that makes warn-mode hook-signature offenders
impossible to miss (#125). Also lands the sync/background parity contract
matrix (#111) — test-only, but it pins the cross-class contracts consumer
migrations depend on.

### Added — bindings registry + system checks (#125)

- **`ProcessManager.bindings`** — a public registry of
  `(model, process_class, state_field)` recorded by every
  `bind_model_process` call, so consumer tooling (coverage audits,
  contract tests) no longer re-derives bindings from model attributes.
- **`django_logic.W001` system check** — re-runs hook-signature
  validation over the registry through Django's checks framework, so
  warn-mode offenders surface in `manage.py check`, every test run and
  deploy checks. Bind-time logger warnings alone are emitted during
  `ready()`, before logging is configured, and can go entirely unseen —
  a consumer's warn-mode suite showed zero warnings on a tree where
  strict mode found three real offenders.

## [0.5.1] — 2026-07-17

### Fixed

- **0.5.0 regression:** bind-time hook validation crashed with
  `TypeError: 'property' object is not iterable` on a Process whose
  class-level `conditions`/`permissions` is a property/descriptor
  (computed per instance). Such definitions cannot be inspected at bind
  time and are now skipped (#121).

## [0.5.0] — 2026-07-17

Type-faithful background kwargs (#107, #108), bind-time hook-signature
validation (#113), and a Django-version CI matrix plus a downstream
consumer-contract job (#110, #112).

### Upgrade notes

The typed kwargs round-trip **changes what background hooks receive in
phase 2** — this is the headline behavioural change of this release:

- **Audit background hooks written against the 0.4.x contract.** Hooks
  that parse ISO strings back into `datetime`/`UUID`, or re-wrap values
  (`UUID(kwargs['some_id'])`), now receive the original types directly.
  While pre-upgrade rows drain, a hook can see *both* forms — tolerate
  both (e.g. `v if isinstance(v, UUID) else UUID(v)`) until the queue is
  clean, then simplify to the typed form.
- **Deploy web and workers together.** A 0.4.x worker passes 0.5.0's
  tagged kwargs dicts through verbatim (it cannot decode them), and
  rolling back to 0.4.x leaves any 0.5.0-written pending rows undecodable.
  Drain or requeue pending `TransitionMessage` rows if you must roll back.
- **Snapshot assertions on persisted kwargs change shape.** The testing
  snapshot helper (`django_logic.testing.snapshot`) exposes the stored
  `TransitionMessage.kwargs`, which now contains `__dl_type__` tag dicts
  for non-JSON-native values.
- **New warnings are on by default.** Passing `request` to a background
  transition, hooks without a named instance-first parameter, and
  non-string dict keys in kwargs each log a warning. Silence them by
  fixing the call sites — or make them hard errors with
  `DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION']` /
  `DJANGO_LOGIC['STRICT_HOOK_SIGNATURES']`.
- **Trove classifiers now match what CI tests**: Django 4.2 / 5.1 / 5.2 /
  6.0. Dropped 4.0 (never installable under the `requires-python >= 3.11`
  floor this package already had) and 5.0 (end-of-life, untested); added
  4.2, which CI has always tested.

### Added — bind-time hook-signature validation (#113)

- `ProcessManager.bind_model_process` now validates every hook across the
  process tree — transition-level side-effects, callbacks, failure hooks,
  conditions and permissions, plus process-level `conditions`/`permissions`:
  the engine calls hooks as `fn(instance, **kwargs)` (permissions as
  `fn(instance, user, **kwargs)`), so a hook whose first parameter is not a
  named positional (e.g. task-style `def hook(*args, **kwargs)`) is flagged
  at bind time instead of failing at runtime on a worker. Warns by default;
  `DJANGO_LOGIC['STRICT_HOOK_SIGNATURES'] = True` raises
  `ImproperlyConfigured`. Decorated hooks need `functools.wraps` so their
  real signature is visible to the validator.

### Changed — kwargs serialization (#107, #108)

- **Type-faithful kwargs round-trip** (#108). Background-transition kwargs
  are now persisted with a self-describing type tag (`__dl_type__`) and
  restored to their original Python types in phase 2: `datetime`, `date`,
  `time`, `Decimal`, `UUID`, `tuple`, `set`, `frozenset` — recursively
  inside containers. A side-effect now receives the same types whether its
  transition is synchronous or background. `Decimal` and `set`, previously
  rejected at phase 1, are now supported. Rows written by older versions
  (plain ISO strings) still decode; deploy web and workers together when
  upgrading across this boundary (an old worker passes tagged dicts through
  verbatim). Model instances remain rejected — pass a pk and re-fetch.
  Non-string dict keys cannot round-trip (JSON objects have string keys):
  phase 1 flags them with a warning, or a `TypeError` under
  `STRICT_KWARGS_SERIALIZATION`.
- **`request` is dropped loudly** (#107). Phase-1 serialization logs a
  warning (with the tr_id) when it drops `request` from a background
  transition's kwargs, and the new
  `DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION'] = True` raises `TypeError`
  (specifically `serializers.KwargsSerializationError`) instead. Phase-2
  hooks must never read `request` — the engine rehydrates `user`; pass
  anything else as plain values.
- New `deserialize_kwargs()` is the phase-2 inverse of
  `serialize_kwargs()`; `restore_user()` remains available.
  `make_json_safe()` is kept as a legacy helper but is no longer used by
  the engine.

## [0.4.1] — 2026-07-02

### Added

- **Advertise Django 6.0 support.** Added the `Framework :: Django :: 6.0`
  trove classifier. This is metadata only — the `django>=4.0` requirement
  already permitted Django 6.0, and the full test suite passes against it.

## [0.4.0] — 2026-07-02

Stability hardening plus condition-disambiguated nested background
transitions (#98) and standardised `AppConfig.ready()` process↔model binding
(#100). Every defect from the 0.3.x stability review (R1–R6 reproduced
defects, D1–D5 design races) is fixed with a permanent regression test. See
`docs/STABILITY_REVIEW_AND_V1_PLAN.md` in the planning repo for the full
findings and resolution mapping.

### Added — nested background transition routing (#98)

- **Condition-disambiguated background transitions across nested processes**
  (issue #98). Two nested processes may now declare background transitions
  that **share an `action_name`**, selected by a condition on the instance —
  the polymorphic-routing pattern the synchronous path already supported
  (e.g. per-integration `Gmail` / `Dummy` sub-processes each owning a
  background `send_message_via_integration`). Phase 1 records the owning
  (nested) process class on the `TransitionMessage`
  (`owning_process_class`, migration `0007`); phase 2 restores that **exact**
  transition from it, without re-evaluating the condition. Generic callers
  keep calling `instance.process.send_message_via_integration(...)`.

### Changed

- **Standardised process↔model binding on `AppConfig.ready()` (issue #100).**
  `ProcessManager.bind_model_process(...)` is now documented and practised in
  exactly one place — the app's `AppConfig.ready()` — instead of at module
  import time in `models.py`/`process.py`. Binding at import time forced a
  `model → process → actions → model` circular import (the process and its
  side-effect/condition/permission functions both reference the model), whose
  only workaround was scattering `from .models import X` calls inside every
  action function. Binding in `ready()` (which runs after every app's models are
  loaded) removes the cycle, so action modules import their model at the top
  level normally. No library API change — `bind_model_process` is unchanged;
  the README, `CLAUDE.md`, the Cursor rule, and the bundled test apps
  (`tests/background`, `tests/stability`) now bind in `ready()` only.
- **`_validate_unique_background_action_names` is relaxed to a single
  invariant.** It previously rejected *any* two background transitions sharing
  an `action_name` across a process and its nested tree, and any background
  name that collided with a synchronous one. It now rejects only the genuinely
  ambiguous case — two **background** transitions sharing an `action_name`
  **within a single process class** (where `(owning class, action_name)` no
  longer identifies one transition). Both a shared background name across
  **distinct** nested process classes, and a background name that **coincides
  with a synchronous** transition, are now allowed: phase 2 only ever restores
  background transitions (`_find_transition` filters to `is_background`), so a
  synchronous namesake is invisible to restore, and phase 1 resolves the call
  by conditions/permissions exactly as it already does for duplicate
  synchronous names (an ambiguous call raises `TransitionNotAllowed` at
  runtime). This enables, e.g., a synchronous fast-path and a durable
  background slow-path under one `action_name`, routed by a condition.
- **Phase-2 restore (`runner._find_transition`) prefers the recorded owner**
  and considers only `is_background` transitions. The owner is recorded for
  every background transition started through the Process entrypoint (for a
  transition on the bound process it equals the bound class). Rows with a blank
  `owning_process_class` — created before this release, or enqueued outside the
  Process entrypoint — fall back to matching by `action_name`, but **only when
  that name is unambiguous across the tree**. If an owner-less (or
  renamed-owner) row's name is shared by several nested background transitions,
  restore **refuses to guess** and finalizes the row without running any
  side-effects (it raises internally and stops retrying) rather than risk
  running the wrong condition-disambiguated sibling. Unique-name legacy rows are
  unaffected.

### Upgrade notes

- **Migration `0007` takes a brief `ACCESS EXCLUSIVE` lock** on
  `transitionmessage` (an additive, non-rewriting `ADD COLUMN` on PostgreSQL
  11+). That table is the engine's hottest, so on a busy system run `migrate`
  with a short `lock_timeout` (e.g. `SET lock_timeout = '2s'`) and retry,
  ideally during a low-throughput window. `owning_process_class` is a
  `TextField` (unbounded, never indexed) so deeply-namespaced process paths
  cannot overflow it.
- **Drain before refactoring a background `action_name` into shared nested
  processes.** If you turn a single, uniquely-named background transition into
  the condition-disambiguated nested pattern (same `action_name` on two nested
  processes), do it in a deploy with **no in-flight rows for that action**
  (or split it across two deploys). A row enqueued by the old code carries a
  blank `owning_process_class`; once the name becomes ambiguous, phase 2 cannot
  determine which sibling it meant and will finalize it without side-effects
  (safe, but the work does not run). Rows enqueued after this release always
  record their owner and are immune.

### Breaking Changes

- **Celery and django-redis are core dependencies.** Background transitions
  are Celery tasks — `celery>=5.0` and `django-redis>=5.0.0` install
  automatically. The `[celery]` / `[redis]` extras remain as empty aliases so
  existing pins keep resolving. The no-Celery `@shared_task` shim is removed.
- **`BACKGROUND_EXECUTION` defaults to `'celery'`** (previously: `'celery'`
  only when Celery was importable, else `'sync'`). Test settings must opt in
  with `DJANGO_LOGIC['BACKGROUND_EXECUTION'] = 'sync'`.
- **Celery mode rejects a per-process lock cache at boot.** With
  `DEBUG=False`, a locmem/dummy `default` cache raises `ImproperlyConfigured`
  (the state lock must be shared between web processes and workers); with
  `DEBUG=True` it logs a warning.
- **The in-flight constraint is scoped per process** (migration `0006`,
  constraint renamed `dl_bg_only_one_uncompleted_per_instance` →
  `dl_bg_one_uncompleted_per_process`). Two processes bound to different
  state fields of one model no longer falsely conflict; a duplicate within
  one process still raises `AlreadyInProgress`.
- **Synchronous transitions are gated on in-flight background work.** While
  an uncompleted `TransitionMessage` exists for an instance + process, a
  synchronous `Transition` on it raises `TransitionNotAllowed` (synchronous
  `Action`s are unaffected). Previously sync and background work could
  interleave and overwrite each other's state writes.
- **Phase-2 side-effects run in a savepoint — failed attempts roll back
  their database writes** (all-or-nothing per attempt). The idempotency
  contract shrinks to external calls only. `failure_side_effects` get the
  same isolation; a broken cleanup path rolls back its partial writes.
- **The phase-2 state guard supersedes externally-moved instances.** If the
  instance no longer sits in the state phase 1 left behind (manual ops fix,
  external write), phase 2 completes the row as superseded (`[superseded]`
  in `last_error_message`), skips side-effects, and the external change
  wins. Configure with `DJANGO_LOGIC['PHASE2_STATE_GUARD'] = 'enforce'`
  (default) or `'warn'` (pre-0.4 behaviour). The same guard protects
  `failed_state` writes by the safety-net tasks.

### Fixed (stability review defects)

- **R1 — a `DatabaseError` raised by a side-effect no longer poisons phase 2.**
  Previously the aborted connection made `record_error` itself raise
  `TransactionManagementError`: the error was never recorded, `errors_count`
  never reached `MAX_ERRORS`, the starter re-dispatched the row forever, and
  the constraint blocked every future background transition on the instance.
  Now it is recorded like any failure and the row reaches its terminal state.
- **R2 — partial side-effect writes from a failed attempt no longer commit**
  (rolled back with the attempt's savepoint).
- **R3 — `RedisState` no longer strands instances locked after background
  transitions.** `RedisState.set_state` writes the cache key with `xx=True`:
  writing state never *creates* a lock key — only `lock()` does. RedisState
  is now fully supported with background transitions.
- **R4 — phase 2 no longer overwrites external state changes** (see the
  state guard above).
- **R5 — false cross-process conflicts removed** (see the per-process
  constraint above).
- **R6 — phase 2 restores the process class that enqueued the transition.**
  `_restore` verifies the attribute-resolved class against the recorded
  `process_class` and prefers the recorded one on mismatch (name collision /
  rename between deploys), using the new `TransitionMessage.field_name`
  instead of guessing the state field.
- **D1 — validate-then-lock TOCTOU closed.** Both sync and background
  phase 1 re-read the persisted state under the lock and reject the
  transition if it is no longer a valid source.
- **D2 — sync/background mutual exclusion.** Background phase 1 acquires the
  state lock for its critical section (released in a `finally`, so nothing
  leaks on `AlreadyInProgress` or a caller-transaction rollback); sync
  transitions check the uncompleted-row gate (see above). Phase 1 also
  re-verifies the persisted state **after** the `TransitionMessage` insert:
  on PostgreSQL the insert can block in a speculative-insert wait while a
  concurrent flight's phase 2 finishes, admitting the request against an
  instance that already reached its target — without the recheck the
  transition silently ran twice (observed live on the Heroku harness).
- **D3 — a failing `Action` no longer clobbers an in-flight transition's
  state.** `failed_state` is written only when the state is not locked;
  otherwise the write is skipped with an ERROR log (the exception still
  propagates and failure hooks still run).

### Fixed (GitHub issues #85–#96)

- **#85 — the state lock is released on every failure path after
  acquisition**: a failed `in_progress_state` write, a failed target write
  in `complete_transition`, and a failed `failed_state` write in
  `fail_transition` all unlock before re-raising. Previously any of these
  froze the instance's FSM for the full `LOCK_TIMEOUT`.
- **#87 — positional arguments to transition methods raise `TypeError`.**
  `instance.process.verify(user)` used to silently drop the positional
  user and run with **no permission checks**.
- **#88 — `in_progress_state` uniqueness is validated across a Process AND
  its nested processes** (matching the documented invariant), not just the
  class's own transitions.
- **#90 — the background runner reloads instances via `_base_manager`**
  (and `State.get_persisted_state` does the same), so a filtered default
  manager (archived/soft-deleted rows hidden) can no longer strand an
  in-flight transition as "unrestorable".
- **#91 — crash re-delivery no longer depends on consumer settings**: every
  django-logic task sets `reject_on_worker_lost=True` alongside
  `acks_late=True` at the task level. The old dispatch-time warning (which
  read the *global* `task_acks_late` and could never fire for the per-task
  setting) is removed.
- **#92 — documented loudly** (README + `AlreadyInProgress` docstring) that
  swallowing `AlreadyInProgress` loses updates that arrive while phase 2 is
  mid-flight, with the dirty-flag/re-dispatch pattern consumers need.
- **#94 — a requested `fail_side_effect` that never fires now fails the
  test loudly**: unknown hook names are rejected eagerly by `track()`, and
  a hook that exists but never executes fails the drive — a silent no-op
  used to turn failure tests into happy-path runs.
- **#95 — snapshot fidelity**: `snapshot()` captures JSONField dict/list
  values as real JSON trees (previously a corrupting Python-repr string)
  and fails loudly on unsupported types; `from_snapshot()` refreshes from
  the DB so the returned instance carries real field types, not strings.
- **#96 — scenario tracking instruments the whole process tree**, so hooks
  executed via `next_transition` follow-ups and callback-triggered
  transitions are visible to `assert_side_effects_ran` /
  `assert_side_effects_not_ran`.
- (#86 validate-then-lock TOCTOU, #89 Action `failed_state` guard, and #93
  sync/background interleaving were fixed by the D1/D3/D2 work above.)

### Added

- **`queue=` is optional.** Transitions without it route to
  `DJANGO_LOGIC['DEFAULT_QUEUE']` (default `'django_logic'`), resolved at
  dispatch time. An explicit empty string is still rejected. `STARTER_QUEUE`
  now defaults to `'django_logic.starter'`.
- **`TransitionMessage.field_name`** — phase 1 records the bound state
  field; phase 2 uses it when reconstructing a process from `process_class`
  (legacy rows fall back to the old inference).
- **`TransitionMessage.mark_as_superseded(note)`** — terminal completion for
  rows superseded by external state changes (no `errors_count` increment).
- **`State.get_persisted_state()`** — always reads the database row,
  bypassing any cache layer; used by the revalidation and the state guard.
- **`docs/TESTING_GUIDE.md`** — the full scenario catalog for testing
  processes (happy paths, gating, failures, retries, terminal failures,
  one-in-flight conflicts, superseded rows, snapshot replay) without Celery.
- **`beat_schedule()`** (`django_logic.background`) — ready-made Celery
  beat entries for the four safety-net tasks, routed to
  `DJANGO_LOGIC['STARTER_QUEUE']` with the recommended intervals
  (overridable per task): `app.conf.beat_schedule = beat_schedule()`.
- **`assert_failure_side_effects_ran` / `assert_failure_callbacks_ran`** on
  `ProcessScenario` — the tracker already recorded failure-hook executions;
  now they are assertable. Snapshots also capture/restore the
  `TransitionMessage.field_name` column so restored rows take the same
  phase-2 path as the production row.

### Observability & DX (from Heroku validation; issues #78–#81)

- **Per-transition monitoring identity.** Background dispatch now sets a Celery
  `shadow` (`django_logic.<app>.<transition>`) so Flower / RabbitMQ management /
  Celery events show a distinct name per transition instead of the one shared
  `django_logic.run_background_transition` task. When `sentry-sdk` is installed,
  the runner also names the Sentry transaction and tags it
  (`dl.app`/`dl.model`/`dl.transition`/`dl.instance_id`/`dl.queue`) per
  transition, so each transition is its own Sentry issue. Opt out with
  `DJANGO_LOGIC['SENTRY_TRANSACTION_NAMING'] = False`. No new dependency.
- **Crash re-delivery configured per task.** Every django-logic task sets
  `acks_late=True` + `reject_on_worker_lost=True` on the decorator (see
  issue #91 above), so the pair crash re-delivery depends on no longer
  hinges on consumer Celery settings. A one-time warning on first
  celery-mode dispatch still flags a missing/in-memory broker.
- **pgbouncer (transaction pooling) deployment guide** in the README
  (`prepare_threshold=None`, `DISABLE_SERVER_SIDE_CURSORS`, no app→pgbouncer SSL).
- **`django_logic.conditions`** — `all_related_in` / `any_related_in` guard
  factories for parent/child completion checks, plus
  `docs/recipes/nested-processes.md` (the clean alternative to nested
  `process.xxx()` calls in side-effects).
- **AI usage rules** — `.cursor/rules/django-logic.mdc` + `CLAUDE.md`.

## [0.3.0]

### Breaking Changes

- **Removed `django_logic.constants` and the `LogType` enum.** All state-change logging now flows through the standard `django-logic` / `django-logic.transition` Python loggers.
- **Removed the legacy logger abstraction.** `AbstractLogger`, `DefaultLogger`, `NullLogger`, `get_logger()`, `DJANGO_LOGIC_DISABLE_LOGGING`, `DJANGO_LOGIC_CUSTOM_LOGGER` are gone. Configure logging through Django `LOGGING` as you would for any other library.
- **Removed `Transition.run_in_background()` / `background_mode` / `background_mode_phase_2` kwargs.** The new `BackgroundTransition` class owns background dispatch end-to-end; there is no per-call opt-in on the base `Transition`.
- **Removed the in-tree `demo/` app** (moved to the separate [django-logic-demo](https://github.com/Borderless360/django-logic-demo) project).
- **DRF moved to an optional dependency** (`pip install django-logic[drf]`). The core library no longer imports Django REST Framework.
- **`in_progress_state` must be unique within a `Process`.** Declaring two transitions on the same process with the same `in_progress_state` now raises `ImproperlyConfigured` at class-creation time.

### New Features — `django_logic.background`

- **`BackgroundTransition` and `BackgroundAction`** — durable, queue-routed background execution with DB persistence (`TransitionMessage`), partial-unique concurrency guard, automatic retry, and a single-task execution model. All side-effects plus the target-state write happen inside one `acks_late=True` Celery task, inside one atomic block.
- **Two execution modes** — `DJANGO_LOGIC['BACKGROUND_EXECUTION']` selects `'celery'` (production) or `'sync'` (tests, management commands, Django shell). Sync mode runs phase 2 inline in the same process, bypasses `transaction.on_commit`, and propagates exceptions to the caller — no Celery broker required for tests.
- **`sync_execution()` context manager** — force Sync mode for a block of code regardless of the global setting.
- **`retry_pending()`** — run the periodic safety-net task once inline, useful for tests that want to simulate "time passed".
- **Explicit queue routing, no default.** Every `BackgroundTransition` must declare `queue='...'`. Missing `queue=` raises `ImproperlyConfigured`. The periodic safety-net tasks run on `DJANGO_LOGIC['STARTER_QUEUE']`.
- **Periodic safety-net tasks** — `retry_stale_transitions`, `cleanup_completed_transitions`, `detect_stuck_transitions`, and `watchdog_stale_attempts`. `retry_stale_transitions` skips rows whose current attempt started within `RETRY_MINUTES` (no per-tick re-dispatch flood while an attempt is in flight).
- **Per-attempt timeouts** — `BackgroundTransition(timeout=<seconds>)` declares a wall-clock budget per phase-2 attempt, persisted as `TransitionMessage.timeout_seconds`. The new `watchdog_stale_attempts` periodic task records a synthetic `TimeoutError` for attempts that exceed it and finalizes the row to `failed_state` once `errors_count` reaches `MAX_ERRORS`. Rows without `timeout` are not watched.
- **Primary-key-agnostic background path** — `TransitionMessage.instance_id` is stored as text (`str(instance.pk)`), so background transitions work with `UUIDField`, `CharField`, and `BigAutoField` primary keys beyond `2**31-1`, matching the synchronous core (migration `0005`).
- **kwargs serialization** — built-in handling of `request`, `user` → `user_id`, `UUID` → `str`, `datetime`/`date` → `.isoformat()`; unserializable values are rejected at phase 1 rather than phase 2.

### Privacy / logging controls

- **Opt-in kwargs redaction.** Transition kwargs (which can carry `user`, `request`, and arbitrary business data) are attached to log records via `extra={'kwargs': ...}`. Two new `DJANGO_LOGIC` settings let PII/compliance-sensitive deployments control this: `LOG_KWARGS = False` omits kwargs from log records entirely, and `LOG_KWARGS_REDACTOR = <callable | 'dotted.path'>` runs a sanitiser over a copy of the kwargs before logging. Default behaviour is unchanged (kwargs logged as-is).

### Observability

- **`TransitionMessage` timing fields** — `started_at`, `completed_at`, `duration_ms`. `started_at` is (re)written at the top of every phase-2 attempt so a watchdog can scan `is_completed=False AND started_at < cutoff` to find hung attempts. `completed_at` is set once when the row is marked completed (success or terminal failure); `duration_ms` measures the last attempt only. Backed by a new `dl_bg_started_idx` index on `(is_completed, started_at)`.

### Bug Fixes

- **Unrestorable `TransitionMessage` rows now stop retrying.** If phase 2 can't restore the instance, process, or transition (e.g. the model was uninstalled, the transition renamed), the TM is now marked `is_completed=True` in its own statement, outside the failed atomic block. Previously the `mark_as_completed()` call was rolled back along with the atomic block, so the periodic starter would re-dispatch the same unrestorable row every `RETRY_MINUTES` forever.
- **Retry safety-net now respects the execution mode.** `_retry_pending_inline` (`retry_stale_transitions` / `retry_pending()`) ran phase 2 inline only via the no-Celery shim; with Celery installed it always called `apply_async`, so in Sync mode a stale row was published to a broker nobody consumes and never retried. It now runs phase 2 inline in Sync mode and re-dispatches via `apply_async` (to the row's own queue) in Celery mode, mirroring `dispatch_transition`.
- **`failure_callbacks` now fire for safety-net-finalized rows.** Rows finalized by `detect_stuck_transitions` / `watchdog_stale_attempts` previously ran `failed_state` + `failure_side_effects` but never `failure_callbacks`, unlike the in-task terminal path. They now run (best-effort, after the finalizing transaction commits) so terminal-failure semantics are identical regardless of which path finalizes the row.
- **`Action.fail_transition` no longer unlocks a lock it never acquired.** A synchronous `Action` skips locking on success but inherited `Transition.fail_transition`'s unconditional `state.unlock()`; a failing `Action` could therefore release the lock a concurrent `Transition` on the same instance/field held (and discard `RedisState`'s cached state). `Action` now has a symmetric, non-unlocking failure path.
- **Background `context` kwarg restored in phase 2.** Side-effects declared as `fn(instance, context, **kwargs)` (the documented signature) raised in background mode because `context` was dropped at phase 1 and never rebuilt. Phase 2 now rebuilds `context={}` like the synchronous path.
- **Phase-1 `IntegrityError` no longer always reported as `AlreadyInProgress`.** The `TransitionMessage` is created before the `in_progress_state` write, so only its partial-unique violation maps to `AlreadyInProgress`; a constraint error from the user's own model write now surfaces as the real `IntegrityError`.
- **Terminal background failures no longer re-raise out of the Celery task.** The phase-2 re-raise is now Sync-mode only — in Celery mode the outcome is fully recorded on the row, so re-raising only spammed task-failure alerts and risked `acks_late` redelivery.
- **`duration_ms` is no longer inflated for safety-net-finalized rows** (it stays null when no real attempt ran), and `get_transition_by_action_name`'s not-found error now uses `instance.pk` (was `.id`, which raised `AttributeError` on custom-PK models).
- **Celery mode warns when no broker is configured.** On the first celery-mode dispatch (when the project's Celery app is actually configured), django-logic logs a one-time warning if the resolved `broker_url` is empty or `memory://` (messages would otherwise vanish into an in-memory transport that no worker drains). The check is at dispatch rather than app-ready because app-ready runs before the standard `celery.py` configures the broker. `_reject_sqlite_in_celery_mode` also now checks only the alias `TransitionMessage` is routed to (a secondary SQLite alias on a Postgres-default deployment is no longer rejected).
- **`errors_count` increments atomically.** `TransitionMessage.record_error` now uses a DB-side `F('errors_count') + 1` update instead of a read-modify-write on a possibly-stale in-memory value, so a watchdog and a reconnected zombie worker racing on the same row can't lose an increment.
- **`NextTransition` no longer guesses on ambiguity.** A `next_transition` whose name resolves to more than one available transition is now refused (logged, skipped) instead of silently running whichever was first in iteration order, and the follow-up is invoked through the normal `Process` entrypoint so it gets its own `tr_id` and `_transition_context` (parent chain) rather than inheriting the parent's.
- **Removed a redundant lock check.** `Transition.change_state` relied on `state.is_locked() or not state.lock()`; the atomic `lock()` alone is sufficient, so the `is_locked()` pre-check (a TOCTOU window + extra round-trip) was dropped.
- **User serialization reads `pk`, not `id`,** matching the phase-2 `get(pk=...)` restore and supporting custom user models whose primary key isn't named `id`.

### Internal cleanup

- **Removed `Transition.get_task_kwargs()`** — replaced by `django_logic.background.serializers.serialize_kwargs` + the `TransitionMessage.kwargs` JSONField.
- **Removed `django_logic.utils` module** (`restore_user_object`, `restore_action`, `get_process_instance`, `get_process_and_state`) — the durable single-task runner owns restoration end-to-end via its own `_restore()` helper.
- **Removed `ProcessManager.bind_state_fields()`**, `ProcessManager.save()`, and `ProcessManager.non_state_fields` (deprecated since 0.2.0).
- **Removed `Process.queryset_name`** and the `queryset_name` parameter on `State`. `State.get_db_state()` uses `model._default_manager` directly.
- **Removed the `ignore_sources` parameter** from `Process.get_available_transitions()` and `Process.get_transition_by_action_name()`. `Transition.__init__` already appends `in_progress_state` to `sources`, so a mid-flight instance finds its own transition without the escape hatch.
- **Removed `TransitionEventType.BACKGROUND_MODE`** and the `_BACKGROUND_MODE_KEYS` filter in `NextTransition` — both were part of the PR #75 fire-and-forget design, now gone.
- **`State.set_state()`** now calls `refresh_from_db(fields=[self.field_name])` instead of a full refresh, so in-memory mutations from side-effects survive the state write.

### Settings

```python
DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'celery',  # or 'sync'
    'STARTER_QUEUE': 'django_logic.starter',  # required in Celery mode
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
```

### Dependencies

- `celery` is now an optional extra (`pip install django-logic[celery]`). The library imports cleanly without it; in Sync mode, Celery is not required at all.

---

## [0.2.0]

### Breaking Changes

- **Dropped Python 3.6–3.10 support.** Minimum required version is now Python 3.11.
- **Removed `setup.py` and `requirements.txt`** — packaging migrated to `pyproject.toml` (setuptools backend).
- **Removed `cached_state` property** from `State`. Use `get_state()` method instead.
- **`State.set_state()` now saves via `instance.save(update_fields=...)`** instead of a raw queryset `update()`, so custom `save()` methods on models are respected.
- **`is_valid()` signature changed** in `Transition` and `BaseTransition`: now accepts `(instance, user)` instead of `(state, user)`.
- **`Conditions.execute()` and `Permissions.execute()`** now accept `instance` instead of `state`.
- **Lock is no longer checked inside `Transition.is_valid()`**; the lock check moved to `Process.get_available_transitions()`.

### New Features

- **`FailureSideEffects`** — new command class and `failure_side_effects` parameter on `Transition`. These run after side-effects fail but _before_ the state is unlocked, allowing cleanup/compensation while the instance is still locked. Execution order on failure: set `failed_state` → failure side-effects → unlock → failure callbacks.
- **Background mode support** — `Transition.change_state()` now supports `background_mode` / `background_mode_phase_2` kwargs, with a `run_in_background()` hook (raises `NotImplementedError` by default, designed for [django-logic-celery](https://github.com/Borderless360/django-logic-celery) integration).
- **Transition context propagation** — each transition receives a unique `tr_id` (UUID). Root/parent IDs (`root_id`, `parent_id`) propagate through nested transitions via thread-safe `ContextVar`, enabling full traceability without explicit kwargs forwarding.
- **New structured logging system** — two standard Python loggers introduced:
  - `django-logic` — general library activity
  - `django-logic.transition` — structured transition event log with `TransitionEventType` enum (`Start`, `Complete`, `Fail`, `SideEffect`, `Callback`, `FailureSideEffect`, `SetState`, `Lock`, `Unlock`, `NextTransition`, `BackgroundMode`)
- **`State.get_state()` method** — reads the current state from the instance attribute (replaces the removed `cached_state` property).
- **`RedisState` rewrite** — now uses a single Redis key for both locking and state storage. The key's existence means locked; its value holds the current state. This makes state changes immediately visible across processes regardless of DB transaction isolation. Automatic TTL-based expiry prevents deadlocks on crashes.
- **Configurable lock timeout** — `DJANGO_LOGIC['LOCK_TIMEOUT']` setting (default: 7200 seconds / 2 hours) replaces the previous hardcoded ~3-year lock duration.
- **`Process.get_transition_by_action_name()`** — new public method to resolve a single transition by action name and user, with clear error handling.
- **`get_available_transitions()` now accepts `ignore_state`** parameter to skip the lock check when needed.
- **`Transition.get_task_kwargs()`** — helper method that serializes transition context (app_label, model_name, instance_id, action_name, etc.) for background task dispatch.
- **`django_logic.utils` module** — new utility functions:
  - `restore_user_object()` — restores user from `user_id` in kwargs
  - `get_process_instance()` — gets process instance from model or `process_class` path
  - `get_process_and_state()` — loads instance + process from serialized kwargs
  - `restore_action()` — restores action from serialized kwargs

### Bug Fixes

- **Root transition exception handling** — exceptions in the root transition are caught and logged instead of propagating to the caller (backward-compatible behavior). Nested transitions still propagate exceptions to parents.
- **`NextTransition` error isolation** — errors in next-transition execution are caught and logged, no longer crashing the main transition.
- **`__getattr__` on `Process`** now strips stale `action_name` from kwargs to prevent "multiple values for argument" errors when kwargs are forwarded from parent transitions.
- Fixed `RedisState.lock()` to store the actual state value (not just `True`) so `get_state()` can return it while locked.

### Deprecations

The legacy logging system is preserved but marked as **DEPRECATED** (will be removed in the next version):
- `LogType` enum in `constants.py`
- `AbstractLogger`, `DefaultLogger`, `NullLogger` classes and `get_logger()` function in `logger.py`
- `DJANGO_LOGIC_DISABLE_LOGGING` and `DJANGO_LOGIC_CUSTOM_LOGGER` settings
- All `self.logger` usage in commands, transitions, and process classes

### Infrastructure & CI

- **Migrated CI from Travis CI to GitHub Actions** (`.github/workflows/ci.yml`), testing on Python 3.11, 3.12, 3.13, 3.14.
- Added `Dockerfile` and `makefile` for local development (build, test, coverage, shell).
- Replaced `setup.py` + `requirements.txt` with `pyproject.toml`.
- Minimum dependencies: Django ≥ 4.0, django-model-utils ≥ 4.5.1, djangorestframework ≥ 3.14.0.

### Tests

- Added `tests/test_logger.py` — comprehensive tests for the new logging system (298 lines).
- Expanded `tests/test_state.py` — added tests for `RedisState`, `get_state()`, lock timeout behavior (+116 lines).
- Expanded `tests/test_transition.py` — added tests for `FailureSideEffects`, background mode, context propagation (+169 lines).
- Added `tests/utils.py` — shared test utilities (125 lines).
- Achieved 100% test coverage.

### Documentation

- Added `docs/logger.md` — documentation for the new structured logging system, including log format, Celery integration, and nested transition examples.
- Updated `README.md` — added CI/coverage badges, documented `failure_side_effects`, updated development setup instructions.
