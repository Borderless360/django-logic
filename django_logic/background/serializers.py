"""kwargs serialization for persisting transition arguments.

``TransitionMessage.kwargs`` is a JSONField, so everything we write must
be JSON-serializable. Values that JSON cannot represent natively are
stored with a self-describing type tag and restored to their original
Python type in phase 2, so a side-effect receives the same types whether
its transition is synchronous or background.

Deliberate handling:

* ``request`` — dropped, loudly: a warning is logged, and
  ``DJANGO_LOGIC['STRICT_KWARGS_SERIALIZATION'] = True`` raises instead.
  A live request cannot cross the phase boundary; extract ``user`` (which
  is rehydrated) or pass plain values.
* ``user`` — replaced with ``user_id`` (restored on the phase-2 side).
* ``datetime`` / ``date`` / ``time`` / ``Decimal`` / ``UUID`` / ``tuple``
  / ``set`` / ``frozenset`` — tag-encoded, restored in phase 2 with the
  original type (recursively, inside dicts/lists/tuples/sets). Two known
  fidelity limits of the isoformat round-trip: a ``ZoneInfo`` tzinfo
  degrades to a fixed-offset ``timezone`` (the UTC instant is preserved,
  the zone identity is not), and ``datetime.fold`` is not preserved.
* ``_transition_context``-managed keys (``tr_id``, ``root_id``,
  ``parent_id``) — stringified when present.
* Model instances and arbitrary objects — rejected at phase 1 via
  ``json.dumps`` (``TypeError``). Pass a pk and re-fetch in the hook:
  phase 2 may run much later and must see fresh rows, not a stale
  snapshot.
* Non-string dict keys — JSON objects only have string keys, so these are
  stringified in storage and do **not** round-trip. Flagged loudly at
  phase 1 (warning, or ``TypeError`` under the strict setting).

.. note::

    Rows written before the typed encoding (plain ISO strings) still
    decode — absence of a tag means passthrough. The inverse is not true:
    a worker running an older version passes the tagged dicts through
    verbatim, so deploy web and workers together when upgrading across
    this boundary.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django_logic.background import settings as bg_settings
from django_logic.logger import transition_logger


class KwargsSerializationError(TypeError):
    """Strict-mode rejection of kwargs that phase 1 would otherwise mutate
    silently (a dropped ``request``, stringified non-string dict keys).

    A ``TypeError`` subclass, so the documented "raises ``TypeError``"
    contract holds — but distinct, so the phase-1 dispatcher re-raises it
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


def make_json_safe(value):
    """Recursively coerce a value into something JSON-serializable.

    Legacy helper (lossy: UUID/datetime become strings, tuples become
    lists). Kept for backward compatibility; :func:`serialize_kwargs` now
    uses the type-preserving :func:`encode_value` instead.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]
    return value


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
            # below rather than crash phase 2 — the raw tagged form stays
            # visible to the side-effect and the log says why.
            transition_logger.warning(
                f"malformed payload for kwargs type tag {tag!r} "
                f"({type(e).__name__}: {e}) — passing value through undecoded"
            )
            return value
        # A row written by a newer version than this worker: pass the
        # tagged form through rather than crash phase 2.
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
    round-trip (a phase-2 hook sees ``'1'`` where the synchronous path saw
    ``1``), so phase 1 flags it loudly instead.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                yield f'{path}[{k!r}]'
            yield from _non_string_key_paths(v, f'{path}[{k!r}]')
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _non_string_key_paths(item, f'{path}[]')


def _non_finite_float_paths(value, path='kwargs'):
    """Yield ``path=value`` for every float that is NaN or +/-Infinity.

    ``json.dumps`` accepts them by default — Python emits the non-standard
    ``NaN``/``Infinity`` tokens — so they pass the phase-1 round-trip guard
    but are not valid JSON: the failure then surfaces backend-dependently
    (PostgreSQL rejects them opaquely at the row write). Phase 1 must
    reject them loudly, naming the offending value.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            yield f'{path}={value!r}'
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _non_finite_float_paths(v, f'{path}[{k!r}]')
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _non_finite_float_paths(item, f'{path}[]')


def serialize_kwargs(kwargs: dict) -> dict:
    """Return a JSON-serializable copy of ``kwargs`` fit for storage.

    Drops ``request`` (warning, or ``TypeError`` under
    ``STRICT_KWARGS_SERIALIZATION``). Replaces ``user`` with ``user_id``.
    Tag-encodes non-JSON-native values so phase 2 restores real types.
    Non-string dict keys are stringified by JSON persistence and cannot
    round-trip — flagged with a warning (or ``TypeError`` under the strict
    setting). Non-finite floats (``float('nan')`` / ``float('inf')``) are
    not valid JSON despite passing ``json.dumps`` — rejected with a
    ``TypeError`` naming the offending value. Raises ``TypeError`` via
    ``json.dumps`` if something unexpected slips through — the caller
    should let that propagate so the failure is visible at phase 1 rather
    than at phase 2.
    """
    out = dict(kwargs)
    if 'request' in out:
        out.pop('request')
        message = (
            f"{out.get('tr_id')} 'request' dropped at kwargs serialization "
            f"— phase-2 hooks must not read it (the engine rehydrates "
            f"'user'; pass anything else as plain values)"
        )
        if bg_settings.strict_kwargs_serialization():
            raise KwargsSerializationError(message)
        transition_logger.warning(message)
    out.pop('context', None)  # rebuilt in phase 2
    # Persisted on its own TransitionMessage column, not in the kwargs JSON:
    # phase 2 reads it from the column, and it must not leak into the kwargs
    # passed to side-effects (it is engine bookkeeping, not caller data).
    out.pop('owning_process_class', None)

    user = out.pop('user', None)
    if user is not None and 'user_id' not in out:
        # Read .pk (not .id) to match the phase-2 restore (get(pk=user_id))
        # and to support custom user models whose primary key isn't named
        # 'id'. AnonymousUser (pk is None) is dropped, as before.
        user_id = getattr(user, 'pk', None)
        if user_id is not None:
            out['user_id'] = user_id

    for key in _CONTEXT_KEYS:
        if key in out and out[key] is not None:
            out[key] = str(out[key])

    bad_keys = sorted(set(_non_string_key_paths(out)))
    if bad_keys:
        message = (
            f"{out.get('tr_id')} non-string dict keys in background "
            f"transition kwargs ({', '.join(bad_keys)}) are stringified by "
            f"JSON persistence — a phase-2 hook sees '1' where the "
            f"synchronous path saw 1, and colliding keys ({{1: …, '1': …}}) "
            f"silently lose data. Use string keys, or a list of pairs."
        )
        if bg_settings.strict_kwargs_serialization():
            raise KwargsSerializationError(message)
        transition_logger.warning(message)

    bad_floats = sorted(set(_non_finite_float_paths(out)))
    if bad_floats:
        raise TypeError(
            f"{out.get('tr_id')} non-finite float in background transition "
            f"kwargs ({', '.join(bad_floats)}): NaN/Infinity pass Python's "
            f"json.dumps but are not valid JSON, so the failure would "
            f"surface backend-dependently at the row write. Pass None, or "
            f"encode the sentinel explicitly."
        )

    out = encode_value(out)

    # Round-trip through json to surface any remaining non-serializable
    # types at phase 1. Cheap on small dicts and invaluable in tests.
    # allow_nan=False so a non-finite float the scan above could not reach
    # (e.g. one hiding in a dict key) still fails here rather than at the
    # row write; translated to TypeError to keep the dispatcher contract
    # (ImproperlyConfigured wraps TypeError, not ValueError).
    try:
        json.dumps(out, allow_nan=False)
    except ValueError as e:
        raise TypeError(
            f"{out.get('tr_id')} kwargs are not valid JSON: {e}"
        ) from e
    return out


def deserialize_kwargs(raw: dict | None) -> dict:
    """Phase-2 inverse of :func:`serialize_kwargs`.

    Restores tag-encoded values to their original Python types and swaps
    ``user_id`` back for a live ``user``.
    """
    kwargs = decode_value(dict(raw or {}))
    restore_user(kwargs)
    return kwargs


def restore_user(kwargs: dict) -> None:
    """In-place: if ``user_id`` is set, swap it for a live ``user`` object.

    Called in phase 2 (via :func:`deserialize_kwargs`). No-op if
    ``user_id`` is absent.
    """
    user_id = kwargs.pop('user_id', None)
    if user_id is None:
        return

    from django.contrib.auth import get_user_model
    try:
        kwargs['user'] = get_user_model().objects.get(pk=user_id)
    except get_user_model().DoesNotExist:
        # The user disappeared between phases; leave user=None so
        # permission checks treat the work as system-initiated.
        kwargs['user'] = None
