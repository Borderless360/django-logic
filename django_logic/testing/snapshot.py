"""State capture & restore — close the loop between production bugs and tests.

``snapshot(instance)`` serialises an instance (its concrete fields, current
state, the related ``TransitionMessage`` if any, and process status) to a plain
JSON-able dict. ``from_snapshot(data)`` rebuilds that instance — and restores
the ``TransitionMessage`` — so a production bug can be reproduced in a test and
kept as a regression guard.

Scope: own concrete fields + the TransitionMessage are captured and restored.
Arbitrary related graphs are not auto-created — build them in the test when a
repro needs them.
"""
from __future__ import annotations

import datetime
import decimal
import json
import uuid


def _jsonable(value):
    """Convert a model-field value to a JSON-able equivalent that Django
    coerces back to the right type on save.

    Dicts/lists (JSONField values) pass through recursively — stringifying
    them produced a Python repr that round-tripped as a corrupted string
    column (issue #95). Anything unsupported fails loudly rather than being
    silently captured as ``str(value)``.
    """
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    raise TypeError(
        f'snapshot: unsupported field value type '
        f'{type(value).__name__!r} ({value!r}). Supported: str/int/float/'
        f'bool/None, datetime/date/time, Decimal, UUID, and JSON-able '
        f'dict/list trees. Exclude the field or convert it yourself before '
        f'snapshotting.'
    )


def snapshot(instance, *, state_field: str = 'status', process_name: str = 'process') -> dict:
    """Capture the full reproducible state of ``instance`` as a JSON-able dict."""
    fields = {}
    for field in instance._meta.concrete_fields:
        fields[field.attname] = _jsonable(field.value_from_object(instance))

    data = {
        'model': instance._meta.label,                       # "app.Model"
        'pk': _jsonable(instance.pk),
        'state_field': state_field,
        'state': getattr(instance, state_field, None),
        'fields': fields,
    }

    # The most recent TransitionMessage for this instance (if the background
    # app is installed and a row exists).
    try:
        from django_logic.testing.runner import latest_message
        tm = latest_message(instance)
        if tm is not None:
            data['transition_message'] = {
                'transition_name': tm.transition_name,
                'process_name': tm.process_name,
                'field_name': tm.field_name,
                'queue_name': tm.queue_name,
                'is_completed': tm.is_completed,
                'errors_count': tm.errors_count,
                'last_error_message': tm.last_error_message,
                'timeout_seconds': tm.timeout_seconds,
                'kwargs': tm.kwargs,
            }
    except Exception:
        pass

    # Best-effort process status.
    try:
        process = getattr(instance, process_name)
        data['process'] = {
            'class': f'{type(process).__module__}.{type(process).__name__}',
            'available_actions': process.get_available_actions(),
            'is_locked': process.state.is_locked(),
        }
    except Exception:
        pass

    return data


def to_json(instance, **kwargs) -> str:
    """``snapshot()`` rendered as an indented JSON string (for logs/Sentry)."""
    return json.dumps(snapshot(instance, **kwargs), indent=2, default=str)


def _load(data_or_path):
    if isinstance(data_or_path, dict):
        return data_or_path
    with open(data_or_path) as fh:
        return json.load(fh)


def from_snapshot(data_or_path, *, model=None):
    """Rebuild an instance (and its TransitionMessage, if captured) from a
    snapshot. Returns the saved instance."""
    data = _load(data_or_path)

    if model is None:
        from django.apps import apps
        model = apps.get_model(data['model'])

    instance = model()
    for attname, value in (data.get('fields') or {}).items():
        try:
            setattr(instance, attname, value)
        except Exception:
            pass
    # Ensure the state field reflects the snapshot even if it wasn't a concrete
    # field name match.
    state_field = data.get('state_field', 'status')
    if 'state' in data and data['state'] is not None:
        setattr(instance, state_field, data['state'])
    if data.get('pk') is not None:
        instance.pk = data['pk']
    instance.save(force_insert=True)
    # The setattrs above wrote serialized forms (ISO strings, str Decimals);
    # the save coerced them in the DATABASE, but the in-memory instance still
    # carries the strings — a condition like ``if instance.band:`` would see
    # ``bool('0.000') == True`` where production saw ``bool(Decimal('0.000'))
    # == False`` (issue #95). Re-read so attributes are real field types.
    instance.refresh_from_db()

    tm_data = data.get('transition_message')
    if tm_data:
        from django_logic.background.models import TransitionMessage
        from django_logic.background import settings as bg_settings
        TransitionMessage.objects.create(
            app_label=instance._meta.app_label,
            model_name=instance._meta.model_name,
            instance_id=str(instance.pk),
            process_name=tm_data.get('process_name', 'process'),
            # Restore the recorded field so phase 2 takes the same
            # recorded-field path the production row would have used
            # ('' = legacy pre-0.4 row, inference fallback).
            field_name=tm_data.get('field_name', ''),
            transition_name=tm_data['transition_name'],
            queue_name=tm_data.get('queue_name') or bg_settings.default_queue(),
            is_completed=tm_data.get('is_completed', False),
            errors_count=tm_data.get('errors_count', 0),
            last_error_message=tm_data.get('last_error_message', ''),
            timeout_seconds=tm_data.get('timeout_seconds'),
            kwargs=tm_data.get('kwargs') or {},
        )

    return instance
