# TODO

Planned changes for upcoming versions of django-logic.

---

## 1.0.0

- [ ] Admin integration module
- [ ] `manage.py transition_status` management command
- [ ] Better error messages (include current state + available transitions)
- [ ] Automated PyPI publishing on tag push
- [ ] Full type annotations (`mypy --strict`)
- [ ] Docs site (MkDocs Material)

## Ops affordances

Carried over from the Heroku validation round.

- [ ] A management command that sends one `TransitionMessage` to the queue
      again straight away, ignoring the `RETRY_MINUTES` recency guard, so an
      operator can act during an incident. The same command lists the
      transitions that are in progress or stuck.
- [ ] Document the Postgres **connection budget**. Each running task holds one
      connection, or two when the app opens a second one per task. Size
      `concurrency × workers` against the database limit (pgbouncer or plan
      cap).
- [ ] Document how to alert when beat stops running — for example Sentry cron
      monitors with `CeleryIntegration(monitor_beat_tasks=True)`. The system
      check that reports a schedule without the recovery tasks is related.
- [ ] Lower the log level for outcomes the engine already handles.
      `detect_stuck` completing a row, the watchdog timeout, and the
      "cannot be restored" path all log at ERROR, which fills Sentry with
      handled cases.

## Later

- [ ] Durable callbacks — an opt-in column on `TransitionMessage` that records
      how far the row got, so a crash cannot lose the callbacks
- [ ] Non-Celery backends (RQ, Dramatiq) behind a pluggable dispatcher interface
- [ ] `django-logic-viz` (Mermaid/Graphviz from process definitions)
- [ ] `django-logic-history` (generalised audit log)
