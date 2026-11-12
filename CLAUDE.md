# django-logic — guidance for AI assistants

This repository contains Django Logic, a declarative workflow library.
Read [README.md](README.md) for declarations and supported deployment settings.
This file records contribution rules and reliability decisions.

## Voice — simplified English, everywhere

This library was largely written by AI tools, and much of the prose became a
private dialect: numbered "phases", "liveness", "retry horizon", ticket ids in
comments. 0.14.0 removed it. **Do not bring it back.**

Write simplified technical English, the way ASD-STE100 defines it:

- One idea per sentence. Aim for 20 words. Never pass 25.
- Active voice, present tense. "The worker writes the row", not "the row is
  written by the worker".
- One word for one meaning. Do not use two words for the same thing.
- Use a verb, not a noun built from a verb. "when it fails", not "on failure of".
- No metaphors and no jokes. Say the mechanism.
- Names are full words: `transition_message`, not `tm`.
- A comment gives a non-obvious *why* in one or two sentences. It never narrates
  the next line, and it never cites a GitHub issue, a PR, or a check id (`#195`,
  `W002`). `CHANGELOG.md` owns that history.
- Do not point at a design-document section number (`§2.7`, `D2 (c)`,
  `contract 7`). State the rule itself in one clause.
- Exception and log text is for the operator on call at 3am. Say what happened
  and what to do.
- If you would not say it out loud, rewrite it.
- When you touch a file, rewrite dialect in the lines you touch.

**Allowed words:** transition, background transition, `TransitionMessage`,
source state, retry window, stranded (nothing is retrying it), enqueue (save
the row and send it to the queue), execute (the worker runs the side-effects),
uncompleted, in progress.

**Retired words, and what to write instead:**

| Do not write | Write |
|---|---|
| `phase 1`, `phase one`, `phase-1` | enqueue |
| `phase 2`, `Phase2`, `phase-2` | execute |
| `liveness` | whether the row is still being retried |
| `retry horizon` | retry window |
| `re-drive`, `redrive` | re-dispatch |
| `in-flight marker` | the uncompleted row |
| `speculative-insert` | describe the insert wait plainly |
| `owning process` | the process that declares the transition |
| `finishing flight` | an attempt that is still running |
| `TM`, `TM-scoped` | `TransitionMessage`, scoped to the row |
| `tm`, `msg`, `inst` | `transition_message`, `message`, `instance` |

`tests/test_voice.py` checks selected retired phrases in the library and current
docs. Reviewers check the remaining wording rules and abbreviations. It skips `CHANGELOG.md` on purpose: the changelog is a historical
record, so it must keep naming the words and APIs that shipped at the time.

### Writing to people — no riddles

Explain behavior in ordinary language. Do not use process lifecycle jargon
in replies, PR text, issues, release notes, docs, or explanatory comments.
Never write "a reaped child frees its worker," "harvest children," or
"the accounting write lands." Say "When a job process finishes, the worker
can start another job" or "The worker records the failure after the database
lock is released." Keep exact code identifiers when needed, but explain
their effect instead of using them as prose.

A summary, a pull-request description, a review reply, or a chat message must
stand on its own. The reader did not watch the work happen.

- Say the thing, not a name for the thing. Write "the two engine cleanups we
  postponed", never a label from an earlier report.
- Do not coin labels for plans, findings, or work items. No letters, no code
  names, no wave or phase numbers. If a name is unavoidable, define it in the
  same message, every time it appears.
- Short is good only when the meaning survives. Cut what the reader does not
  need. Do not replace an explanation with a token the reader must decode.
- Before you send, ask: does every word work for someone who did not watch
  the work? If not, rewrite.

**Declarations are specifications.** Write every process and its transitions
out in full — explicit `sources`, `target`, `conditions`, `side_effects`,
`callbacks` per declaration, even when sibling processes repeat one shape.
Duplication in declarations is acceptable and usually preferable: it tells
how the process behaves; a builder that assembles transitions tells how the
code works and hides the line a reviewer came to check. The same rule holds
where the process is used: write `instance.process.action(...)` literally,
never `getattr(process, action_name)` with the name held in a variable.

## The architecture rule (locked — do not drift back)

One rule decides every reliability design here: **the cache may lie briefly
(a lock key expires); the database row never lies (it is written atomically
with the state change); everything recoverable recovers from a row.**
Its corollaries, each pinned by a shipped mistake:

- **No durable busy marker without a durable owner.** A busy state written
  to the database with no row that owns its recovery parks the instance
  forever when a worker is hard-killed. That design shipped once, grew a
  sweeper whose defects dominated four review passes, and was cut in
  0.12.0. Do not rebuild it.
- **PostgreSQL protects state through the actual transaction commit.**
  State changes take the business-row lock and wait at most two seconds for
  another transaction to commit before they refuse. The shared cache lock also
  prevents overlapping external calls while its timeout remains valid.
  Saving a background job writes no cache lock on PostgreSQL. A process killed
  during that save once left the record locked until the lock timeout.
  Workers separately lock their job row. Finalization takes
  the job lock before the business row. Enqueue must not wait for an unfinished
  job while holding its business row. Allow for a separate connection when
  provider receipts use `atomic=False`. See `docs/operation-contract.md`.
- **The row names its transition.** Recovery re-runs work from
  `TransitionMessage` rows, never from broker messages, so the row records
  `transition_name` and `owning_process_class`. The row is also the queue:
  workers claim it directly (`SELECT FOR UPDATE SKIP LOCKED`), so nothing
  mirrors it into a broker message that could be lost, duplicated, or
  published to a queue nobody consumes.

## Release policy (anti-spiral)

Measured on 0.10.0 → 0.13.1: +1,263 net engine lines in 13 days, ~90% of it
guarding the machinery's own failure modes; 68.9% of inserted lines fixed the
same release's own fixes. Therefore:

1. **No hardening without a consumer-reproduced failure.** A guard, knob,
   validator or doc lands only for a defect a consumer hit or reproduced —
   never because a review imagined one. Corollary: do not validate,
   document or optimize a knob until a consumer sets it.
2. **Adversarial self-review is capped at one pass per release.** Findings
   from later passes become issues for the next consumer-driven release;
   they are not fixed in place.
3. **A third fix on the same defect class triggers a design cut, not a
   fourth fix.** Precedent: 0.12.0 removed the stranded sweep (−582) after
   four passes kept finding defects there — the cut ended the incident
   class where hardening had not.
4. **Ship new public options with the consumer change that uses them.**
   This also applies to columns and settings. The CHANGELOG entry names
   that consumer and explains what the change fixes.

## Working IN this repo

- Tests: `python tests/manage.py test` (SQLite suite);
  PostgreSQL concurrency + stability suites under `tests/stability`,
  `tests/background`. There is no pytest configuration — do not add one
  without wiring `DJANGO_SETTINGS_MODULE`.
- `django_logic/background/` is the durable engine: `transitions.py`
  (enqueue), `pull.py` (the worker loop), `runner.py` (execute on the
  worker), `safety_nets.py`, `models.py` (`TransitionMessage`),
  `serializers.py` (kwargs round-trip). Settings readers live in
  `django_logic/conf.py`.
- Read `docs/design/PULL_WORKERS.md` and
  `docs/recipes/nested-processes.md` (the fan-out pattern and the
  cascading-failure anti-pattern it replaces) before changing the
  background engine. Release tags retain removed design records.
- `CHANGELOG.md` is the record of what shipped and why. Open GitHub issues
  hold what has not. Do not add a TODO.md or planning docs that duplicate
  issues.
