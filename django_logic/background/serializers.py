"""Store background job arguments as plain JSON and restore the submitting user."""
from __future__ import annotations

import math
from uuid import UUID

from django.db import router

from django_logic.background.exceptions import _RestoreError


class KwargsSerializationError(TypeError):
    """Rejection of kwargs that enqueue would otherwise mutate silently
    (a caller-supplied ``user_id``, unstorable text, or non-string dict keys).

    A ``TypeError`` subclass, so the documented "raises ``TypeError``"
    contract holds — but distinct, so the enqueue dispatcher re-raises it
    as-is instead of wrapping it in the generic "not JSON-serializable"
    ``ImproperlyConfigured``.
    """


_CONTEXT_KEYS = ('tr_id', 'root_id', 'parent_id')


def _check_job_value(value, path, containers):
    if isinstance(value, str):
        try:
            value.encode('utf-8')
            if '\x00' not in value:
                return
        except UnicodeEncodeError:
            pass
        raise KwargsSerializationError(f'{path} contains characters the database cannot store (NUL or a lone surrogate).')
    if value is None or isinstance(value, (int, bool)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if not isinstance(value, (dict, list)):
        raise TypeError(f'kwargs are not valid JSON at {path}: {type(value).__name__}.')
    if id(value) in containers:
        raise KwargsSerializationError(f'{path} contains a circular reference.')
    containers.add(id(value))
    try:
        if isinstance(value, list):
            for index, item in enumerate(value):
                _check_job_value(item, f'{path}[{index}]', containers)
        else:
            if '__dl_type__' in value:
                raise KwargsSerializationError(
                    f'{path} contains a legacy typed job argument. Finish these jobs on the previous release.'
                )
            for key, item in value.items():
                if not isinstance(key, str):
                    raise KwargsSerializationError(f'{path} has non-string dict keys. Use strings for JSON keys.')
                _check_job_value(key, f'{path} key {key!r}', containers)
                _check_job_value(item, f'{path}[{key!r}]', containers)
    finally:
        containers.remove(id(value))


def serialize_kwargs(kwargs: dict) -> dict:
    """Prepare plain JSON and replace a user with its primary key."""
    out = dict(kwargs)
    # The worker restores ``user_id`` as a user object, so it cannot carry business data.
    if 'user_id' in out:
        raise KwargsSerializationError(
            f"{out.get('tr_id')} 'user_id' cannot be passed to a background "
            f"transition — it is the engine's wire form for 'user' and the "
            f"worker replaces it with a live user object, so a caller-supplied "
            f"value could never reach a hook. Pass it under a different name."
        )
    out.pop('context', None)  # rebuilt in the worker
    # Store the declaring class once; the worker reconstructs this hook argument.
    out.pop('owning_process_class', None)

    user = out.pop('user', None)
    if user is not None:
        # Read .pk (not .id) to match the worker restore (get(pk=user_id))
        # and to support custom user models whose primary key isn't named
        # 'id'. AnonymousUser (pk is None) is not saved.
        user_id = getattr(user, 'pk', None)
        if user_id is not None:
            out['user_id'] = str(user_id) if isinstance(user_id, UUID) else user_id

    for key in _CONTEXT_KEYS:
        if key in out and out[key] is not None:
            out[key] = str(out[key])

    _check_job_value(out, 'kwargs', set())
    return out


def deserialize_kwargs(raw: dict | None) -> dict:
    """Refuse jobs saved with old type tags before invoking business code."""
    kwargs = dict(raw or {})
    _check_job_value(kwargs, 'kwargs', set())
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
    model = get_user_model()
    try:
        # A replica can miss a new user or still contain a deleted account.
        kwargs['user'] = model.objects.using(router.db_for_write(model)).get(pk=user_id)
    except model.DoesNotExist as error:
        raise _RestoreError(
            f'The submitting user {user_id!r} no longer exists. '
            'Review the job before submitting replacement work under an authorized user.'
        ) from error
