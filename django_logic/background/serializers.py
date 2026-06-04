"""kwargs serialization for persisting transition arguments.

``TransitionMessage.kwargs`` is a JSONField, so everything we write must
be JSON-serializable. This module handles the common non-serializable
values that transitions receive.

Deliberate handling:

* ``request`` — dropped. Extract ``user`` first if you need it.
* ``user`` — replaced with ``user_id`` (restored on the phase-2 side).
* ``UUID`` — stringified.
* ``datetime`` / ``date`` — ISO-formatted.
* ``_transition_context``-managed keys (``tr_id``, ``root_id``,
  ``parent_id``) — stringified when present.

Any unrecognised type falls through to ``json.dumps`` which will raise
``TypeError`` — we surface that as an ``ImproperlyConfigured``-style
error at phase-1 time rather than letting it fail at phase-2 fetch time.

.. warning::

    **The round-trip is lossy and types are NOT preserved.** A background
    transition's side-effects receive a *different Python type* for some
    kwargs than the identical synchronous transition would:

    * ``datetime`` / ``date`` → ``str`` (ISO 8601)
    * ``UUID`` → ``str``
    * ``tuple`` → ``list``

    Only ``user`` is rehydrated in phase 2 (``user_id`` → live ``User``).
    There is no inverse for the rest, because the JSON column does not
    record the original type. So a side-effect that does ``when.date()`` or
    ``some_id.hex`` works synchronously and raises ``AttributeError`` in the
    background path. Write background side-effects to accept the serialized
    forms (parse the ISO string / re-wrap the UUID yourself), or pass an
    already-stringified value. ``set``, ``Decimal``, and model instances are
    rejected outright at phase 1 (see :func:`serialize_kwargs`) — pass a
    list / str / pk instead.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from uuid import UUID


_CONTEXT_KEYS = ('tr_id', 'root_id', 'parent_id')


def make_json_safe(value):
    """Recursively convert a value into something JSON-serializable."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]
    return value


def serialize_kwargs(kwargs: dict) -> dict:
    """Return a JSON-serializable copy of ``kwargs`` fit for storage.

    Drops ``request`` entirely. Replaces ``user`` with ``user_id``.
    Stringifies UUIDs and datetimes. Raises ``TypeError`` via
    ``json.dumps`` if something unexpected slips through — the caller
    should let that propagate so the failure is visible at phase 1
    rather than at phase 2.
    """
    out = dict(kwargs)
    out.pop('request', None)
    out.pop('context', None)  # rebuilt in phase 2

    user = out.pop('user', None)
    if user is not None and 'user_id' not in out:
        user_id = getattr(user, 'id', None)
        if user_id is not None:
            out['user_id'] = user_id

    for key in _CONTEXT_KEYS:
        if key in out and out[key] is not None:
            out[key] = str(out[key])

    out = make_json_safe(out)

    # Round-trip through json to surface any remaining non-serializable
    # types at phase 1. Cheap on small dicts and invaluable in tests.
    json.dumps(out)
    return out


def restore_user(kwargs: dict) -> None:
    """In-place: if ``user_id`` is set, swap it for a live ``user`` object.

    Called at the top of phase 2. No-op if ``user_id`` is absent.
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
