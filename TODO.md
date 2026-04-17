# TODO

Planned changes for upcoming versions of django-logic.

---

## 0.3.0 — COMPLETE

- [x] Remove legacy logging (`LogType`, `AbstractLogger`, `DefaultLogger`, `NullLogger`, `get_logger()`)
- [x] Remove `DJANGO_LOGIC_DISABLE_LOGGING` / `DJANGO_LOGIC_CUSTOM_LOGGER` settings
- [x] Remove all `self.logger` references from commands, transitions, process
- [x] DRF and Celery as optional extras
- [x] Remove `background_mode` / `run_in_background` from base `Transition`
- [x] Ship `django_logic.background` (`BackgroundTransition`, `BackgroundAction`)
- [x] TransitionMessage model + migrations, partial unique constraint, retry/cleanup periodic tasks
- [x] Sync execution mode + `sync_execution()` context manager
- [x] Class-time validation: required `queue=`, unique `in_progress_state` within a Process
- [x] Move in-tree demo to the `django-logic-demo` repo

## 1.0.0

- [ ] Scenario-based testing framework (`django_logic.testing`): `ProcessScenario`, snapshot/replay, AI-readable failure output
- [ ] Admin + DRF integration modules
- [ ] Execution timing instrumentation (`started_at`, `completed_at`, `duration_ms` on `TransitionMessage`)
- [ ] Configurable per-transition timeouts
- [ ] `manage.py transition_status` management command
- [ ] Better error messages (include current state + available transitions)
- [ ] Automated PyPI publishing on tag push
- [ ] Full type annotations (`mypy --strict`)
- [ ] Docs site (MkDocs Material)

## Later

- [ ] Durable callbacks (opt-in `phase` column on `TransitionMessage`)
- [ ] Non-Celery backends (RQ, Dramatiq) behind a pluggable dispatcher interface
- [ ] `django-logic-viz` (Mermaid/Graphviz from process definitions)
- [ ] `django-logic-history` (generalised audit log)
