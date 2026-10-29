"""Optional submission identity and nonblocking cancellation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json

from django.db import OperationalError, transaction, router, DEFAULT_DB_ALIAS

from django_logic.background.exceptions import RequestConflict
from django_logic.background.serializers import TYPE_TAG, decode_value, encode_value, serialize_kwargs


@dataclass
class Submission:
    request_key: str | None = None
    fairness_key: str | None = None
    job_id: int | None = None
    created: bool = False
    fingerprint: str = ''
    consumed: bool = False


_options = ContextVar('django_logic_operation_options', default=None)


def validate_key(value, name, *, allow_empty=False):
    if value is not None and (not isinstance(value, str) or len(value) > 255
                              or ('\x00' in value) or (not value and not allow_empty)):
        raise ValueError(f'{name} must be a string of 1 to 255 characters.')
    return value


@contextmanager
def operation_options(*, request_key=None, fairness_key=None):
    """Apply metadata to one background submission. Existing action returns stay unchanged."""
    validate_key(request_key, 'request_key')
    if request_key is not None and request_key.startswith('django-logic:'):
        raise ValueError('The django-logic: request-key prefix is reserved for required successors.')
    submission = Submission(
        request_key,
        validate_key(fairness_key, 'fairness_key', allow_empty=True),
    )
    token = _options.set(submission)
    try:
        yield submission
    finally:
        _options.reset(token)


def prepare_submission(process, action_name, kwargs):
    """Resolve retained requests before the object's state can reject a repeat."""
    submission = _options.get()
    if submission is None or submission.consumed:
        return None, None
    from django_logic.background.models import TransitionMessage
    submission.consumed = True
    payload = serialize_kwargs({key: value for key, value in kwargs.items()
                                if key not in {'context', 'tr_id', 'root_id', 'parent_id',
                                               'process_class', 'owning_process_class'}})
    identity = TransitionMessage.instance_key(process.state.instance, process.state.process_name)
    identity['database'] = process.state.instance._state.db or DEFAULT_DB_ALIAS
    payload = {
        'instance': identity, 'field': process.state.field_name,
        'process_class': f'{type(process).__module__}.{type(process).__qualname__}',
        'action': action_name, 'kwargs': payload,
    }
    submission.fingerprint = sha256(json.dumps(_canonical(payload), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if submission.request_key is None:
        return submission, None
    using = router.db_for_write(TransitionMessage) or DEFAULT_DB_ALIAS
    existing = TransitionMessage.objects.using(using).filter(request_key=submission.request_key).first()
    if existing is not None:
        record_submission(submission, existing, created=False)
    return submission, existing


def _canonical(value):
    """Set iteration order must not change a request's identity across processes."""
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        if value.get(TYPE_TAG) == 'dict':
            # The inner dictionary is caller data, even when it resembles a type tag.
            return {TYPE_TAG: 'dict', 'value': {
                key: _canonical(item) for key, item in value['value'].items()
            }}
        result = {key: _canonical(item) for key, item in value.items()}
        if result.get(TYPE_TAG) in ('set', 'frozenset'):
            result['value'] = sorted(result['value'], key=lambda item: json.dumps(item, sort_keys=True))
        return result
    return value


def record_submission(submission, row, *, created):
    if submission is None:
        return
    if row.request_fingerprint != submission.fingerprint:
        raise RequestConflict('This request key already identifies different operation inputs.')
    submission.job_id = row.pk
    submission.created = created


def verify_internal_request(row, state, transition_name, serialized, owner):
    """A required successor key must never name unrelated work."""
    from django_logic.background.models import TransitionMessage
    expected = TransitionMessage.instance_key(state.instance, state.process_name)
    expected.update(field_name=state.field_name, transition_name=transition_name, owning_process_class=owner)
    same_identity = all(getattr(row, field) == value for field, value in expected.items())
    previous = {key: value for key, value in decode_value(row.kwargs).items() if key != 'tr_id'}
    current = {key: value for key, value in decode_value(serialized).items() if key != 'tr_id'}
    if not same_identity or _canonical(encode_value(previous)) != _canonical(encode_value(current)):
        raise RequestConflict('The required successor key already identifies different operation inputs.')


def cancel_pending(job_id, *, using='default'):
    """Cancel without waiting for a running job. Cancellation does not change business state."""
    from django_logic.background.models import TransitionMessage

    try:
        with transaction.atomic(using=using):
            row = TransitionMessage.objects.using(using).select_for_update(nowait=True).filter(pk=job_id).first()
            if row is None:
                return 'missing'
            if row.is_completed:
                return 'finished'
            row.mark_as_completed(measure_duration=False, completion_kind='cancelled')
            return 'cancelled'
    except OperationalError as error:
        cause = error.__cause__
        if (getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None)) == '55P03':
            return 'running'
        raise
