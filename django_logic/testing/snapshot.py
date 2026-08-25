"""State capture & restore — close the loop between production bugs and tests.

``snapshot(instance)`` serialises an instance (its concrete fields, current
state, and the related ``TransitionMessage`` if any) to a plain
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

    # The most recent TransitionMessage for this instance's process (if the
    # background app is installed and a row exists). Scoped by process_name so
    # a second process bound to another state field of the same model can't
    # leak its row into this snapshot.
    try:
        from django_logic.testing.runner import latest_message
        transition_message = latest_message(instance, process_name=process_name)
        if transition_message is not None:
            data['transition_message'] = {
                'transition_name': transition_message.transition_name,
                'process_name': transition_message.process_name,
                # The class the caller drove. Captured from the row, not
                # derived from the snapshot instance: the row lookup keys
                # on the concrete model, so a snapshot taken through
                # another class of the same table must still replay the
                # class production ran.
                'proxy_model_label': transition_message.proxy_model_label,
                'field_name': transition_message.field_name,
                'owning_process_class': transition_message.owning_process_class,
                'queue_name': transition_message.queue_name,
                'is_completed': transition_message.is_completed,
                'errors_count': transition_message.errors_count,
                'last_error_message': transition_message.last_error_message,
                'timeout_seconds': transition_message.timeout_seconds,
                'kwargs': transition_message.kwargs,
                # The retry clock: the retry backoff, the retry
                # classification and the stuck report all read these, so
                # a hung production row must replay as hung, not pristine.
                'last_error_dt': _jsonable(transition_message.last_error_dt),
                'failure_side_effect_error': transition_message.failure_side_effect_error,
                'started_at': _jsonable(transition_message.started_at),
                'completed_at': _jsonable(transition_message.completed_at),
                'duration_ms': transition_message.duration_ms,
            }
    except Exception:
        pass

    return data


def _load(data_or_path):
    if isinstance(data_or_path, dict):
        return data_or_path
    with open(data_or_path) as fh:
        return json.load(fh)


def _restore_dt(value):
    """ISO string (as captured) -> ``datetime``. The DB adapter would coerce
    the string too, but the created row is read back by callers, so keep the
    in-memory attributes real field types."""
    if not value:
        return None
    if isinstance(value, str):
        from django.utils.dateparse import parse_datetime
        return parse_datetime(value)
    return value


def from_snapshot(data_or_path, *, model=None):
    """Rebuild an instance (and its TransitionMessage, if captured) from a
    snapshot. Returns the saved instance."""
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

    tm_data = data.get('transition_message')
    if tm_data:
        from django_logic.background.models import TransitionMessage
        from django_logic import conf
        tm_process_name = tm_data.get('process_name', 'process')
        # The snapshot IS this instance+process's background state. Leaving an
        # existing row behind either replays a stale orphan (the older row wins
        # every lookup ordered by id... or loses, unpredictably) or trips the
        # uncompleted-per-(instance, process) unique constraint with a cryptic
        # IntegrityError instead of restoring.
        TransitionMessage.for_instance(instance, tm_process_name).delete()
        TransitionMessage.objects.create(
            **TransitionMessage.instance_key(instance, tm_process_name),
            # The captured value wins; deriving from the snapshot instance
            # is only for snapshots taken before the column existed.
            proxy_model_label=tm_data.get(
                'proxy_model_label',
                TransitionMessage.proxy_label_for(instance)),
            # Restore the recorded field so the worker takes the same
            # recorded-field path the production row would have used
            # ('' = legacy pre-0.4 row, inference fallback).
            field_name=tm_data.get('field_name', ''),
            transition_name=tm_data['transition_name'],
            # Restore the owning-process discriminator so worker replay
            # resolves the exact transition (blank on legacy snapshots →
            # first-match fallback, unchanged behaviour).
            owning_process_class=tm_data.get('owning_process_class', ''),
            queue_name=tm_data.get('queue_name') or conf.default_queue(),
            is_completed=tm_data.get('is_completed', False),
            errors_count=tm_data.get('errors_count', 0),
            last_error_message=tm_data.get('last_error_message', ''),
            timeout_seconds=tm_data.get('timeout_seconds'),
            kwargs=tm_data.get('kwargs') or {},
            # The retry clock again — see snapshot(): a hung row must
            # replay as hung, not pristine.
            last_error_dt=_restore_dt(tm_data.get('last_error_dt')),
            failure_side_effect_error=tm_data.get(
                'failure_side_effect_error', ''),
            started_at=_restore_dt(tm_data.get('started_at')),
            completed_at=_restore_dt(tm_data.get('completed_at')),
            duration_ms=tm_data.get('duration_ms'),
        )

    return instance
