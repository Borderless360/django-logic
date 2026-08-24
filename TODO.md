# TODO

Planned changes for upcoming versions of django-logic.

---

## 1.0.0

Breaking cleanup. Land it as one release with a migration guide in
`CHANGELOG.md`. The consumer must reach 0.16.x before upgrading.

- [ ] **One transition type, one lock/gate/chain contract.** Today a sync
      `Action` takes no lock, skips the in-flight gate, and ignores
      `next_transition`, while `BackgroundAction` does all three (the README
      carries the reference table). Fold the four types into one class where
      `target=None` means "no success write"; keep the durable variant as
      the smaller break for consumers.
- [ ] **Drop the migration bridges.** `LEGACY_EXCEPTION_BASE` (blocked until
      gv finishes the coupled-core port and drops the setting), the pre-0.4
      restore fallbacks (blank `field_name` / `owning_process_class` rows),
      the empty `[celery]` extra, and `DEFER_UNLOCK_UNTIL_COMMIT` if still
      no consumer sets it.
- [ ] **Default the strict flags to True, then remove them.** Run the gv
      suite with `STRICT_HOOK_SIGNATURES` and `STRICT_KWARGS_SERIALIZATION`
      on first.
- [ ] **Collapse the exceptions no consumer catches by name.** Keep
      `TransitionTemporarilyUnavailable` vs `TransitionNotAllowed` (409 vs
      400). gv catches nothing else by name.
- [ ] **Trim the swap hooks.** `permissions_class` stays (gv's insurance
      app sets it on the new engine); drop the other `*_class` hooks if
      still unused.
- [ ] **Freeze `nested_processes`.** They earn their keep for one pattern —
      same action name, mutually exclusive conditions, one bound accessor.
      No new restore fallbacks; fan-out stays separate machines.
- [ ] **Squash migrations 0001–0009 into one initial migration.** Give the
      squash a distinct name (a squash named `0001_initial` that lists
      itself in `replaces` removes itself) and the full `replaces` list.
      Rename `0008_transitionmessage_dispatch_marker` inside the squash —
      renaming an applied migration outside one ghosts it in every
      consumer's `django_migrations`.
- [ ] **The lock-identity rework — change the key exactly once.** Add the
      database alias (today one row on two databases shares a lock key; a
      per-tenant-database consumer gets cross-tenant "State is locked"
      refusals) and key the model identity on the concrete table (an MTI
      parent and child sharing one state column lock under different keys).
      Record the alias on `TransitionMessage` and route `_restore` by it.
      The key is shared-cache identity across rolling deploys, so this
      needs its own release with a drain-or-accept-one-TTL-window note.
- [ ] Admin integration module
- [ ] `manage.py transition_status` management command
- [ ] Better error messages (include current state + available transitions)
- [ ] Automated PyPI publishing on tag push
- [ ] Full type annotations (`mypy --strict`)
- [ ] Docs site (MkDocs Material) — then trim the README's ~500-line
      background section to install, quick start, one example, pointers

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
- [ ] Document how to alert when `dl_worker` stops running — the safety nets
      run inside the worker loop, so a dead worker also means no safety nets.
- [ ] Lower the log level for outcomes the engine already handles.
      `detect_stuck` completing a row, the timeout kill, and the
      "cannot be restored" path all log at ERROR, which fills Sentry with
      handled cases.

## Later

- [ ] Durable callbacks — an opt-in column on `TransitionMessage` that records
      how far the row got, so a crash cannot lose the callbacks
- [ ] `django-logic-viz` (Mermaid/Graphviz from process definitions)
- [ ] `django-logic-history` (generalised audit log)
