"""Capture and restore a model's own fields for a regression test.

Related records and background jobs are explicit test fixtures. Use
open_transition_message when a reproduction needs an existing job.
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
    column. Anything unsupported fails loudly rather than being
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


def snapshot(instance, *, state_field: str = 'status') -> dict:
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

    return data


def _load(data_or_path):
    if isinstance(data_or_path, dict):
        return data_or_path
    with open(data_or_path) as fh:
        return json.load(fh)


def from_snapshot(data_or_path, *, model=None):
    """Rebuild a model's own fields and return the saved instance."""
    data = _load(data_or_path)

    if model is None:
        from django.apps import apps
        model = apps.get_model(data['model'])

    recorded = data.get('model')
    if recorded and recorded.lower() != model._meta.label.lower():
        # Field names of a different model mostly do not overlap, so the
        # unknown-field drop below would silently corrupt the restore.
        raise ValueError(
            f'snapshot: captured from {recorded!r} but restoring into '
            f'{model._meta.label!r}. Pass the model the snapshot came from '
            f'(or a scenario whose ``model`` is {recorded!r}).'
        )

    instance = model()
    for attname, value in (data.get('fields') or {}).items():
        # No try/except: ``fields`` comes from the model's own concrete fields,
        # so a name that does not exist means the snapshot does not belong to
        # this model — which the label check above already rejects loudly.
        setattr(instance, attname, value)
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
    # == False``. Re-read so attributes are real field types.
    instance.refresh_from_db()

    return instance
