# TODO

Planned changes for upcoming versions of django-logic.

---

## 1.0.0

- [ ] Admin + DRF integration modules
- [ ] `manage.py transition_status` management command
- [ ] Better error messages (include current state + available transitions)
- [ ] Automated PyPI publishing on tag push
- [ ] Full type annotations (`mypy --strict`)
- [ ] Docs site (MkDocs Material)

## Ops affordances

Carried over from the Heroku validation round.

- [ ] A management command to re-dispatch a specific `TransitionMessage`
      immediately, bypassing the `RETRY_MINUTES` recency guard (incident
      response), and to list in-progress / stuck transitions.
- [ ] Document the Postgres **connection budget**: each in-flight task holds a
      connection (two if the app opens a second one per task), so size
      `concurrency × workers` against the DB limit (pgbouncer or plan cap).
- [ ] Document a beat-liveness alert recipe — e.g. Sentry cron monitors via
      `CeleryIntegration(monitor_beat_tasks=True)`. See also the system check
      added for a schedule that never installed the safety-net entries.
- [ ] Log level for handled safety-net conditions: `detect_stuck`
      finalization, the watchdog timeout and the "cannot be restored" path log
      at ERROR, which is Sentry noise for handled outcomes (#154).

## Later

- [ ] Durable callbacks (opt-in `phase` column on `TransitionMessage`)
- [ ] Non-Celery backends (RQ, Dramatiq) behind a pluggable dispatcher interface
- [ ] `django-logic-viz` (Mermaid/Graphviz from process definitions)
- [ ] `django-logic-history` (generalised audit log)
