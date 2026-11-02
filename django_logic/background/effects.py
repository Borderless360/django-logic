"""Independent work saved when a background transition completes."""
from uuid import uuid4
from hashlib import sha256

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, router, transaction

from django_logic.background.transitions import BackgroundTransition


class BackgroundEffect:
    """A named callable with its own durable job, retries and completion.

    The callable receives the model instance and the parent's saved arguments.
    It must check whether its business work still applies: later state changes
    do not cancel an effect, and the engine never writes the parent's state for it.
    """

    def __init__(self, name, action, *, queue=None, timeout=None, atomic=True, no_retry_on=()):
        if not isinstance(name, str) or not name or len(name) > 100 or '\x00' in name:
            raise ImproperlyConfigured('BackgroundEffect name must contain 1 to 100 characters.')
        if not callable(action):
            raise ImproperlyConfigured('BackgroundEffect action must be callable.')
        # Apply the same option validation as ordinary background work.
        validated = _EffectTransition(name, sources=[], queue=queue, timeout=timeout,
                                      atomic=atomic, no_retry_on=no_retry_on)
        self.name = name
        self.action = action
        self.queue = validated.queue
        self.timeout = validated.timeout
        self.atomic = validated.atomic
        self.no_retry_on = validated.no_retry_on

    def as_transition(self):
        return _EffectTransition(
            self.name, sources=[], side_effects=[self.action], queue=self.queue,
            timeout=self.timeout, atomic=self.atomic, no_retry_on=self.no_retry_on,
        )


class _EffectTransition(BackgroundTransition):
    is_independent_effect = True


def validate_effect_database(instance):
    """Refuse routing that the existing worker cannot execute on one database."""
    from django_logic.background.models import TransitionMessage

    aliases = {
        instance._state.db or DEFAULT_DB_ALIAS,
        router.db_for_read(type(instance)), router.db_for_write(type(instance), instance=instance),
        router.db_for_read(TransitionMessage), router.db_for_write(TransitionMessage),
    }
    if aliases != {DEFAULT_DB_ALIAS}:
        raise ImproperlyConfigured(
            'Background effects require the model and jobs to use the default database for reads and writes.'
        )


def validate_effect_submission(transition, state, kwargs, *, using=None):
    """Apply the same checks to normal submissions and required successors."""
    if not transition.background_effects:
        return
    from django_logic.background.non_atomic import validate_non_atomic
    from django.db import connections

    original_alias = state.instance._state.db
    # Non-atomic completion uses an engine-owned copy of the default connection.
    # Routing still concerns the normal connection used for application writes.
    if using and using.startswith('_django_logic_job_') and original_alias == using:
        if (connections[using].settings_dict['NAME'] != connections[DEFAULT_DB_ALIAS].settings_dict['NAME']
                or connections[using].settings_dict['ENGINE'] != connections[DEFAULT_DB_ALIAS].settings_dict['ENGINE']):
            raise ImproperlyConfigured('Background effect completion must use the default database.')
        state.instance._state.db = DEFAULT_DB_ALIAS
    try:
        validate_effect_database(state.instance)
        if not kwargs.get('owning_process_class'):
            raise ImproperlyConfigured('Background effects must be submitted through a bound Process action.')
        for effect in transition.background_effects:
            if not effect.atomic:
                # Effects run after the enclosing commit in sync mode.
                validate_non_atomic(state.instance, require_autocommit=False)
    finally:
        state.instance._state.db = original_alias


def save_effect_jobs(parent, transition, state, kwargs):
    """Save every declared effect on the connection completing its parent."""
    from django_logic.background.models import TransitionMessage
    from django_logic.background.operations import verify_internal_request
    from django_logic.background.serializers import serialize_kwargs

    using = parent._state.db or DEFAULT_DB_ALIAS
    rows = TransitionMessage.objects.using(using)
    for name in parent.background_effect_names:
        effect = transition.get_background_effect(name)
        child_kwargs = dict(kwargs)
        child_kwargs.pop('context', None)
        child_kwargs.pop('user_id', None)
        child_kwargs.update(parent_id=kwargs.get('tr_id'), tr_id=uuid4())
        serialized = serialize_kwargs(child_kwargs)
        request_key = f'django-logic:effect:{parent.pk}:{name}'
        # Old workers cannot mistake this row for the parent's state-changing action.
        transition_name = 'django-logic:effect:' + sha256(name.encode()).hexdigest()
        child, created = rows.get_or_create(request_key=request_key, defaults={
            **TransitionMessage.instance_key(state.instance, parent.process_name),
            'proxy_model_label': parent.proxy_model_label,
            'field_name': parent.field_name,
            'transition_name': transition_name,
            'parent_transition_name': parent.transition_name,
            'owning_process_class': parent.owning_process_class,
            'parent_job_id': parent.pk,
            'effect_name': name,
            'fairness_key': parent.fairness_key,
            'queue_name': effect.queue or parent.queue_name,
            'timeout_seconds': effect.timeout,
            'kwargs': serialized,
        })
        if not created:
            verify_internal_request(child, state, transition_name, serialized, parent.owning_process_class)
            if (child.effect_name != name or child.parent_job_id != parent.pk
                    or child.parent_transition_name != parent.transition_name):
                raise ImproperlyConfigured('The background effect request key identifies another job.')


def dispatch_effect_jobs(parent_job_id):
    """Run inline effects after parent completion; pull workers use the saved rows."""
    from django_logic import conf
    from django_logic.background.models import TransitionMessage
    from django_logic.background.runner import run_background_transition
    from django_logic.logger import transition_logger

    if not conf.sync_mode():
        from django_logic.background.pull import notify_workers
        transaction.on_commit(notify_workers)
        return

    def run_effects():
        for pk in TransitionMessage.objects.filter(parent_job_id=parent_job_id).order_by('pk').values_list('pk', flat=True):
            try:
                run_background_transition(pk)
            except Exception as error:
                # The child has its own failure record. Its parent already succeeded.
                transition_logger.error('Background effect job %s failed: %s', pk, type(error).__name__)

    # A caller rollback must never run an effect whose saved intent disappears.
    transaction.on_commit(run_effects)
