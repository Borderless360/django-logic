# Django Logic — Documentation Index

> All planning, design, and reference material for the road to 1,000 stars.

---

## Structure

```
dl/
├── fundamental problem.md            ← repo root: nested-transition
│                                        failure analysis (GV upgrade pain)
│
└── docs/
    ├── INDEX.md                      ← you are here
    ├── PLAN.md                       ← master execution plan (stages 1–5)
    ├── TESTING_GUIDE.md              ← how to test your processes:
    │                                   journeys-not-mirrors + scenario catalog
    │
    ├── design/                       ← active design decisions
    │   ├── BACKGROUND_TRANSITION_ANALYSIS.md   ← chosen design, crash
    │   │                                         matrix, queue strategy
    │   └── TESTING_SCENARIOS.md                ← scenario-based testing
    │                                             framework
    │
    └── research/                     ← historical research & raw notes
        ├── PR-75-REVIEW.md           ← review of PR #75 (Stage 1, complete)
        ├── idea1.txt                 ← monitoring, timeouts, fallback ideas
        └── race-condition-issue      ← race-condition investigation & fix
```

---

## Reading Order

For someone new to this project, read in this order:

1. **[`fundamental problem.md`](../fundamental%20problem.md)** (repo root) —
   Why nested `process.xxx()` calls inside side-effects cascaded when
   `django-logic` started re-raising in 0.2.0. This is the operational
   reason the Stage 2 design exists.

2. **[PLAN.md](PLAN.md)** — The master plan. Vision, current state,
   Stages 1–5, resolved decisions, success metrics.

3. **[design/BACKGROUND_TRANSITION_ANALYSIS.md](design/BACKGROUND_TRANSITION_ANALYSIS.md)** —
   The chosen `BackgroundTransition` design in detail: single-task
   execution, explicit per-transition queues, crash-point analysis,
   reliability contract. Primary input for Stage 2 implementation.

4. **[design/TESTING_SCENARIOS.md](design/TESTING_SCENARIOS.md)** —
   Scenario-based testing framework for document-driven development.
   `ProcessScenario` API, AI-readable output, state snapshots.
   Primary input for Stage 3.

5. **[TESTING_GUIDE.md](TESTING_GUIDE.md)** — The practical how-to for
   testing your own processes: the *journeys, not mirrors* principle and
   guardrails, the full scenario catalog (gating, failure paths, the
   re-raise/swallow contract, domain-outcome assertions, the cross-machine
   cascade), and the `ProcessScenario` API reference.

6. **[research/](research/)** — Historical notes: PR #75 review
   (Stage 1 complete), race-condition investigation, monitoring ideas.

---

## Progress

| Stage | Version | Status |
|-------|---------|--------|
| Stage 1 — Land PR #75 | v0.2.0 | Complete |
| Stage 2 — Durable BackgroundTransition | v0.3.0 | Active |
| Stage 3 — Observability, DX & Testing | v1.0.0 | Planned |
| Stage 4 — Communication & Launch | — | Planned |
| Stage 5 — Community & Ecosystem | — | Planned |

---

## Document Purposes

| Folder | Purpose | Modify? |
|--------|---------|---------|
| `docs/` (root) | Master plan and index | Yes — update as stages complete |
| `docs/design/` | Active design decisions we're building from | Yes — refine as we implement |
| `docs/research/` | Historical research, completed reviews, raw notes | No — keep as-is for reference |
