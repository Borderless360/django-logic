# Django Logic — Documentation Index

Documentation lives in two clearly separated tiers: **current** user-facing
guides (normative — kept in sync with the shipped code) and **historical**
planning/research material (kept for context, not normative).

---

## Current documentation — start here

User-facing guides for the shipped release (see [CHANGELOG.md](../CHANGELOG.md)
for what each version delivered):

1. **[README.md](../README.md)** (repo root) — installation, quick start,
   core concepts, background transitions, production deployment. The primary
   user guide.
2. **[TESTING_GUIDE.md](TESTING_GUIDE.md)** — how to test your processes:
   the *journeys, not mirrors* principle, the full scenario catalog (gating,
   failure paths, retries, superseded rows, nested processes, snapshot
   replay), and the `ProcessScenario` API reference.
3. **[recipes/nested-processes.md](recipes/nested-processes.md)** — the
   parent/child fan-out recipe: how to coordinate work across state machines
   without the cascading-failure anti-pattern (nested `process.xxx()` calls
   inside side-effects).
4. **[logger.md](logger.md)** — structured logging: the `django-logic` /
   `django-logic.transition` loggers and how to configure them via Django
   `LOGGING`.
5. **[IMPROVEMENTS_FROM_HEROKU_VALIDATION.md](IMPROVEMENTS_FROM_HEROKU_VALIDATION.md)**
   — validated-behavior notes and open improvement ideas from the
   production-style Heroku validation (RabbitMQ + PostgreSQL + worker crashes
   + pgbouncer).

---

## Historical — kept for context, not normative

Planning and research material from the 0.2 → 0.8 development push. The work
these documents plan and analyse **has shipped** (durable background
transitions in 0.3.0/0.4.0, scenario testing in 0.4.0, observability in 0.6.0,
transition coverage in 0.8.0); where they disagree with README/TESTING_GUIDE
or the code, the shipped behaviour wins.

- **[PLAN.md](PLAN.md)** — snapshot of the v3 execution plan (Stages 1–5),
  superseded by the shipped 0.4–0.8 releases; see the CHANGELOG.
- **[design/BACKGROUND_TRANSITION_ANALYSIS.md](design/BACKGROUND_TRANSITION_ANALYSIS.md)**
  — the design record for `BackgroundTransition`: single-task execution,
  crash-point analysis, queue strategy, reliability contract.
- **[design/TESTING_SCENARIOS.md](design/TESTING_SCENARIOS.md)** — the design
  record for the scenario-based testing framework (`ProcessScenario`,
  AI-readable output, snapshots).
- **[research/](research/)** — raw notes: PR #75 review (Stage 1),
  race-condition investigation, monitoring/timeout/fallback ideas.

> The original "fundamental problem" write-up (the nested-transition failure
> analysis) was an external research note and is **not part of this repo**;
> its shipped equivalent is
> [recipes/nested-processes.md](recipes/nested-processes.md).

---

## Structure

```
django-logic/
├── README.md                         ← current: primary user guide
├── CHANGELOG.md                      ← current: per-release history
│
└── docs/
    ├── INDEX.md                      ← you are here
    ├── TESTING_GUIDE.md              ← current: how to test your processes
    ├── logger.md                     ← current: structured logging
    ├── IMPROVEMENTS_FROM_HEROKU_VALIDATION.md
    │                                 ← current: validated behavior + ideas
    ├── recipes/
    │   └── nested-processes.md       ← current: parent/child fan-out recipe
    │
    ├── PLAN.md                       ← historical: v3 execution plan snapshot
    ├── design/                       ← historical: design decision records
    │   ├── BACKGROUND_TRANSITION_ANALYSIS.md
    │   └── TESTING_SCENARIOS.md
    └── research/                     ← historical: raw research notes
        ├── PR-75-REVIEW.md
        ├── idea1.txt
        └── race-condition-issue
```

---

## Progress

| Stage | Version | Status |
|-------|---------|--------|
| Stage 1 — Land PR #75 | v0.2.0 | Complete |
| Stage 2 — Durable BackgroundTransition | v0.3.0–v0.4.0 | Complete (shipped) |
| Stage 3 — Observability, DX & Testing | v0.4.0 (scenario testing), v0.6.0 (observability), v0.8.0 (transition coverage) | Complete (shipped) |
| Stage 4 — Communication & Launch | — | Planned |
| Stage 5 — Community & Ecosystem | — | Planned |

See [CHANGELOG.md](../CHANGELOG.md) for the authoritative per-release record
and [TODO.md](../TODO.md) for what remains planned.

---

## Document Purposes

| Folder | Purpose | Modify? |
|--------|---------|---------|
| `docs/` (root) | Index + current user guides (TESTING_GUIDE, logger, recipes) | Yes — keep in sync with shipped code |
| `docs/PLAN.md` | Historical plan snapshot | No — banner + link fixes only |
| `docs/design/` | Historical design decision records (implemented) | No — keep as-is for reference |
| `docs/research/` | Historical research, completed reviews, raw notes | No — keep as-is for reference |
