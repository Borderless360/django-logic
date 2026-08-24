"""kwargs serialization for persisting transition arguments.

``TransitionMessage.kwargs`` is a JSONField, so everything we write must
be JSON-serializable. Values that JSON cannot represent natively are
stored with a self-describing type tag and restored to their original
Python type in the worker, so a side-effect receives the same types whether
its transition is synchronous or background.

Deliberate handling:

* ``request`` — dropped, loudly: a warning is logged, and
  ``DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION'] = True`` raises instead.
  A live request cannot cross the queue; extract ``user`` (which
  is rehydrated) or pass plain values.
* ``user`` — replaced with ``user_id`` (restored on the worker side).
* ``datetime`` / ``date`` / ``time`` / ``Decimal`` / ``UUID`` / ``tuple``
  / ``set`` / ``frozenset`` — tag-encoded, restored in the worker with the
  original type (recursively, inside dicts/lists/tuples/sets). Two known
  fidelity limits of the isoformat round-trip: a ``ZoneInfo`` tzinfo
  degrades to a fixed-offset ``timezone`` (the UTC instant is preserved,
  the zone identity is not), and ``datetime.fold`` is not preserved.
* ``_transition_context``-managed keys (``tr_id``, ``root_id``,
  ``parent_id``) — stringified when present.
* Model instances and arbitrary objects — rejected at enqueue via
  ``json.dumps`` (``TypeError``). Pass a pk and re-fetch in the hook:
  the worker may run much later and must see fresh rows, not a stale
  snapshot.
* Non-string dict keys — JSON objects only have string keys, so these are
  stringified in storage and do **not** round-trip. Flagged loudly at
  enqueue (warning, or ``TypeError`` under the strict setting).

.. note::

    Rows written before the typed encoding (plain ISO strings) still
    decode — absence of a tag means passthrough. The inverse is not true:
    a worker running an older version passes the tagged dicts through
    verbatim, so deploy web and workers together when upgrading across
    this boundary.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django_logic import conf
from django_logic.logger import transition_logger


class KwargsSerializationError(TypeError):
    """Strict-mode rejection of kwargs that enqueue would otherwise mutate
    silently (a dropped ``request``, stringified non-string dict keys).

    A ``TypeError`` subclass, so the documented "raises ``TypeError``"
    contract holds — but distinct, so the enqueue dispatcher re-raises it
    as-is instead of wrapping it in the generic "not JSON-serializable"
    ``ImproperlyConfigured``.
    """


_CONTEXT_KEYS = ('tr_id', 'root_id', 'parent_id')

#: Marker key for tag-encoded values. A caller dict that happens to contain
#: this key is escaped with the ``'dict'`` tag so it round-trips verbatim.
TYPE_TAG = '__dl_type__'

_SCALAR_DECODERS = {
    'datetime': datetime.fromisoformat,
    'date': date.fromisoformat,
    'time': time.fromisoformat,
    'decimal': Decimal,
    'uuid': UUID,
}


def encode_value(value):
    """Recursively encode a value into tagged, JSON-serializable form."""
    # datetime before date: datetime is a date subclass.
    if isinstance(value, datetime):
        return {TYPE_TAG: 'datetime', 'value': value.isoformat()}
    if isinstance(value, date):
        return {TYPE_TAG: 'date', 'value': value.isoformat()}
    if isinstance(value, time):
        return {TYPE_TAG: 'time', 'value': value.isoformat()}
    if isinstance(value, Decimal):
        return {TYPE_TAG: 'decimal', 'value': str(value)}
    if isinstance(value, UUID):
        return {TYPE_TAG: 'uuid', 'value': str(value)}
    if isinstance(value, tuple):
        return {TYPE_TAG: 'tuple', 'value': [encode_value(v) for v in value]}
    if isinstance(value, frozenset):
        return {TYPE_TAG: 'frozenset', 'value': [encode_value(v) for v in value]}
    if isinstance(value, set):
        return {TYPE_TAG: 'set', 'value': [encode_value(v) for v in value]}
    if isinstance(value, dict):
        encoded = {k: encode_value(v) for k, v in value.items()}
        if TYPE_TAG in value:
            return {TYPE_TAG: 'dict', 'value': encoded}
        return encoded
    if isinstance(value, list):
        return [encode_value(v) for v in value]
    return value


def decode_value(value):
    """Inverse of :func:`encode_value`; untagged values pass through."""
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    if isinstance(value, dict):
        tag = value.get(TYPE_TAG)
        if tag is None:
            return {k: decode_value(v) for k, v in value.items()}
        inner = value.get('value')
        try:
            if tag == 'dict':
                return {k: decode_value(v) for k, v in inner.items()}
            if tag == 'tuple':
                return tuple(decode_value(v) for v in inner)
            if tag == 'set':
                return {decode_value(v) for v in inner}
            if tag == 'frozenset':
                return frozenset(decode_value(v) for v in inner)
            decoder = _SCALAR_DECODERS.get(tag)
            if decoder is not None:
                return decoder(inner)
        except Exception as e:
            # A known tag whose payload no longer decodes (hand-edited row,
            # cross-version writer bug): mirror the unknown-tag passthrough
            # below rather than crash the worker — the raw tagged form stays
            # visible to the side-effect and the log says why.
            transition_logger.warning(
                f"malformed payload for kwargs type tag {tag!r} "
                f"({type(e).__name__}: {e}) — passing value through undecoded"
            )
            return value
        # A row written by a newer version than this worker: pass the
        # tagged form through rather than crash the worker.
        transition_logger.warning(
            f"unknown kwargs type tag {tag!r} — passing value through "
            f"undecoded (worker older than the row writer?)"
        )
        return value
    return value


def _non_string_key_paths(value, path='kwargs'):
    """Yield a path for every dict key that is not a ``str``.

    JSON objects only have string keys, so ``{1: 'a'}`` is persisted as
    ``{"1": "a"}`` — silently, since ``json.dumps`` stringifies int/float/
    bool/None keys instead of raising. That breaks the type-faithful
    round-trip (a worker hook sees ``'1'`` where the synchronous path saw
    ``1``), so enqueue flags it loudly instead.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                yield f'{path}[{k!r}]'
            yield from _non_string_key_paths(v, f'{path}[{k!r}]')
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _non_string_key_paths(item, f'{path}[]')


def _unstorable_text_paths(value, path='kwargs'):
    """Yield paths to strings a Postgres jsonb column would reject."""
    if isinstance(value, str):
        if '\x00' in value:
            yield path
        else:
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                yield path
    elif isinstance(value, dict):
        for k, v in value.items():
            # Keys too: a NUL in a key breaks the write exactly as a NUL in a
            # value does, and json.dumps encodes both happily.
            yield from _unstorable_text_paths(k, f'{path} key {k!r}')
            yield from _unstorable_text_paths(v, f'{path}[{k!r}]')
    elif isinstance(value, (set, frozenset)):
        for v in value:
            yield from _unstorable_text_paths(v, f'{path}{{...}}')
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _unstorable_text_paths(v, f'{path}[{i}]')


def serialize_kwargs(kwargs: dict) -> dict:
    """Return a JSON-serializable copy of ``kwargs`` fit for storage.

    Drops ``request`` and a caller-supplied ``user_id`` (warning, or
    ``KwargsSerializationError`` under ``STRICT_KWARGS_SERIALIZATION``) —
    both are reserved. Replaces ``user`` with ``user_id``.
    Tag-encodes non-JSON-native values so the worker restores real types.
    Non-string dict keys are stringified by JSON persistence and cannot
    round-trip — flagged with a warning (or ``TypeError`` under the strict
    setting). Non-finite floats (``float('nan')`` / ``float('inf')``) are
    not valid JSON despite passing ``json.dumps`` — rejected with a
    ``TypeError`` naming the offending value. Raises ``TypeError`` via
    ``json.dumps`` if something unexpected slips through — the caller
    should let that propagate so the failure is visible at enqueue rather
    than at the worker.
    """
    out = dict(kwargs)
    if 'request' in out:
        out.pop('request')
        message = (
            f"{out.get('tr_id')} 'request' dropped at kwargs serialization "
            f"— worker hooks must not read it (the engine rehydrates "
            f"'user'; pass anything else as plain values)"
        )
        if conf.strict_kwargs_serialization():
            raise KwargsSerializationError(message)
        transition_logger.warning(message)
    # ``user_id`` is the engine's own wire form for ``user`` (restored in
    # the worker by restore_user). A caller passing it as ordinary data used to
    # be silently consumed: restore_user popped it and replaced it with a
    # live ``user``, so the hook never saw the value — and the same call ran
    # correctly in sync mode, a parity break that only showed up in
    # production. Treated like ``request``: reserved, dropped loudly.
    if 'user_id' in out:
        out.pop('user_id')
        message = (
            f"{out.get('tr_id')} 'user_id' dropped at kwargs serialization "
            f"— it is the engine's wire form for 'user' and the worker replaces "
            f"it with a live user object, so a caller-supplied value could "
            f"never reach a hook. Pass it under a different name."
        )
        if conf.strict_kwargs_serialization():
            raise KwargsSerializationError(message)
        transition_logger.warning(message)
    out.pop('context', None)  # rebuilt in the worker
    # Persisted on its own TransitionMessage column, not in the kwargs JSON:
    # the worker reads it from the column, and it must not leak into the kwargs
    # passed to side-effects (it is engine bookkeeping, not caller data).
    out.pop('owning_process_class', None)

    user = out.pop('user', None)
    if user is not None:
        # Read .pk (not .id) to match the worker restore (get(pk=user_id))
        # and to support custom user models whose primary key isn't named
        # 'id'. AnonymousUser (pk is None) is dropped, as before.
        user_id = getattr(user, 'pk', None)
        if user_id is not None:
            out['user_id'] = user_id

    for key in _CONTEXT_KEYS:
        if key in out and out[key] is not None:
            out[key] = str(out[key])

    # PostgreSQL jsonb rejects NUL and lone surrogates, which json.dumps
    # happily encodes — the same class of value as the non-finite floats
    # rejected below, and with the same consequence: enqueue would die with a
    # raw backend DataError at the row write instead of a named TypeError here.
    unstorable = sorted(set(_unstorable_text_paths(out)))
    if unstorable:
        raise KwargsSerializationError(
            f"{out.get('tr_id')} background transition kwargs contain "
            f"characters the database cannot store "
            f"({', '.join(unstorable)}): NUL (U+0000) and lone surrogates are "
            f"rejected by PostgreSQL jsonb. Strip or escape them before "
            f"passing the value."
        )

    bad_keys = sorted(set(_non_string_key_paths(out)))
    if bad_keys:
        message = (
            f"{out.get('tr_id')} non-string dict keys in background "
            f"transition kwargs ({', '.join(bad_keys)}) are stringified by "
            f"JSON persistence — a worker hook sees '1' where the "
            f"synchronous path saw 1, and colliding keys ({{1: …, '1': …}}) "
            f"silently lose data. Use string keys, or a list of pairs."
        )
        if conf.strict_kwargs_serialization():
            raise KwargsSerializationError(message)
        transition_logger.warning(message)

    out = encode_value(out)

    # Round-trip through json to surface any remaining non-serializable
    # types at enqueue. Cheap on small dicts and invaluable in tests.
    # allow_nan=False: Python's json emits the non-standard NaN/Infinity
    # tokens by default, which would then fail backend-dependently at the
    # row write. Translated to TypeError to keep the dispatcher contract
    # (ImproperlyConfigured wraps TypeError, not ValueError).
    try:
        json.dumps(out, allow_nan=False)
    except ValueError as e:
        raise TypeError(
            f"{out.get('tr_id')} kwargs are not valid JSON: {e}"
        ) from e
    return out


def deserialize_kwargs(raw: dict | None) -> dict:
    """Worker inverse of :func:`serialize_kwargs`.

    Restores tag-encoded values to their original Python types and swaps
    ``user_id`` back for a live ``user``.
    """
    kwargs = decode_value(dict(raw or {}))
    restore_user(kwargs)
    return kwargs


def restore_user(kwargs: dict) -> None:
    """In-place: if ``user_id`` is set, swap it for a live ``user`` object.

    Called in the worker (via :func:`deserialize_kwargs`). No-op if
    ``user_id`` is absent.
    """
    user_id = kwargs.pop('user_id', None)
    if user_id is None:
        return

    from django.contrib.auth import get_user_model
    try:
        kwargs['user'] = get_user_model().objects.get(pk=user_id)
    except get_user_model().DoesNotExist:
        # The user disappeared between enqueue and execute; leave user=None so
        # permission checks treat the work as system-initiated.
        kwargs['user'] = None
