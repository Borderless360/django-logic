"""Idempotency assertion for background side-effects (issue #106).

Background side-effects re-run FROM SCRATCH on every retry (crash
re-delivery, watchdog requeue, the periodic starter), so every side-effect
must be idempotent: a second application must change nothing observable.
Consumers hand-rolled "call twice, compare" — or skipped the check.
``assert_idempotent`` makes it one call::

    from django_logic.testing import assert_idempotent

    assert_idempotent(send_invoice, invoice,
                      fields=['sent_at'],
                      capture=lambda i: i.emails.count())
"""
from __future__ import annotations

import copy


def assert_idempotent(fn, instance, *, fields=None, capture=None,
                      refresh_from_db=True, **kwargs):
    """Apply ``fn(instance, **kwargs)`` twice and assert the second
    application changes nothing observable.

    Background side-effects re-run from scratch on every retry, so they must
    be idempotent — this pins that contract for one side-effect the way a
    consumer would otherwise hand-roll ("call twice, compare").

    The observation is the named ``fields`` read off ``instance`` (refreshed
    from the DB first unless ``refresh_from_db=False``) plus, when given, the
    result of ``capture(instance)`` — use ``capture`` for effects the
    instance's own columns can't see (related-row counts, mock call_counts).
    At least one of ``fields``/``capture`` is required: with neither, the
    observation is empty and the assertion would pass vacuously. Observed
    values are deep-copied, so mutable values (lists, dicts) are compared by
    value at observation time, not by a shared reference ``fn`` mutated.

    Raises ``AssertionError`` with the first-vs-second observation diff.
    """
    fields = list(fields or ())
    if not fields and capture is None:
        raise TypeError(
            'assert_idempotent requires fields=[...] and/or '
            'capture=callable — with neither, nothing is observed and the '
            'assertion would pass vacuously.')

    def observe():
        if refresh_from_db:
            instance.refresh_from_db()
        obs = {field: getattr(instance, field) for field in fields}
        if capture is not None:
            obs['capture()'] = capture(instance)
        return copy.deepcopy(obs)

    fn(instance, **kwargs)
    first = observe()
    fn(instance, **kwargs)
    second = observe()

    if first != second:
        name = getattr(fn, '__name__', repr(fn))
        diff = [f'{key}: {first[key]!r} -> {second[key]!r}'
                for key in first if first[key] != second[key]]
        raise AssertionError(
            f'{name} is not idempotent — the second application changed '
            'the observation:\n  ' + '\n  '.join(diff) + '\n'
            'Background side-effects re-run from scratch on every retry, '
            'so a second application must change nothing observable.')
