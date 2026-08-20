"""Models and processes for background-transition tests.

This app binds every process in one place: ``BackgroundTestsConfig.ready()``
in ``tests/background/apps.py``. Binding here at import time would recreate
the model to process to actions to model import cycle. ``ready()`` runs after
every app's models are loaded, so it is the only supported binding site.
"""
from django.db import models

from django_logic import Action, Process, Transition
from django_logic.background import BackgroundAction, BackgroundTransition


def bg_ok(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'ok,'
    instance.save(update_fields=['se_log'])


def bg_boom(instance, **kwargs):
    raise ValueError('boom')


def bg_refuse(instance, **kwargs):
    from django_logic.background import PermanentFailure
    raise PermanentFailure('the rule says no')


# Holds the exact kwargs, values and types, that the last side-effect received
# on the worker. Round-trip tests read it instead of a database column.
LAST_KWARGS: dict = {}


def bg_record_kwargs(instance, **kwargs):
    instance.kwargs_seen = sorted(kwargs.keys())
    instance.save(update_fields=['kwargs_seen'])
    LAST_KWARGS.clear()
    LAST_KWARGS.update(kwargs)


def bg_callback(instance, **kwargs):
    instance.cb_log = (instance.cb_log or '') + 'cb,'
    instance.save(update_fields=['cb_log'])


def bg_failure_callback(instance, **kwargs):
    instance.cb_log = (instance.cb_log or '') + 'fcb,'
    instance.save(update_fields=['cb_log'])


class Widget(models.Model):
    status = models.CharField(max_length=32, default='draft')
    # A second state field, driven by WidgetAuditProcess below. Two state
    # machines on one row must be able to run background work at the same
    # time.
    audit_status = models.CharField(max_length=32, default='clean')
    se_log = models.TextField(default='', blank=True)
    cb_log = models.TextField(default='', blank=True)
    kwargs_seen = models.JSONField(default=list, blank=True)

    class Meta:
        app_label = 'bg_tests'


class WidgetProcess(Process):
    process_name = 'process'
    transitions = [
        BackgroundTransition(
            action_name='fulfil',
            sources=['draft'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfilment_failed',
            queue='django_logic.critical',
            side_effects=[bg_ok, bg_record_kwargs],
            callbacks=[bg_callback],
            failure_callbacks=[bg_failure_callback],
        ),
        BackgroundTransition(
            action_name='generate_export',
            sources=['fulfilled'],
            target='exported',
            in_progress_state='exporting',
            failed_state='export_failed',
            queue='django_logic.slow',
            side_effects=[bg_ok],
        ),
        BackgroundTransition(
            action_name='crash',
            sources=['draft'],
            target='crashed_target',
            in_progress_state='crashing',
            failed_state='crash_failed',
            queue='django_logic.critical',
            side_effects=[bg_boom],
            failure_callbacks=[bg_failure_callback],
        ),
        BackgroundTransition(
            action_name='timeboxed',
            sources=['draft'],
            target='tb_done',
            in_progress_state='tb_running',
            failed_state='tb_failed',
            queue='django_logic.slow',
            side_effects=[bg_ok],
            timeout=60,
        ),
        BackgroundTransition(
            action_name='refuse',
            sources=['draft'],
            target='refuse_done',
            in_progress_state='refusing',
            failed_state='refused',
            queue='django_logic.critical',
            side_effects=[bg_refuse],
            failure_callbacks=[bg_failure_callback],
        ),
        BackgroundTransition(
            action_name='refuse_declared',
            sources=['draft'],
            target='rd_done',
            in_progress_state='rd_running',
            failed_state='rd_refused',
            queue='django_logic.critical',
            side_effects=[bg_boom],
            failure_callbacks=[bg_failure_callback],
            no_retry_on=(ValueError,),
        ),
        BackgroundAction(
            action_name='sync_inventory',
            sources=['fulfilled', 'exported'],
            queue='django_logic.fast',
            side_effects=[bg_ok],
            callbacks=[bg_callback],
        ),
        BackgroundAction(
            action_name='crash_action',
            sources=['fulfilled'],
            queue='django_logic.fast',
            failed_state='sync_failed',
            side_effects=[bg_boom],
            failure_callbacks=[bg_failure_callback],
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'fulfilled'],
            target='cancelled',
        ),
    ]


def bg_audit_ok(instance, **kwargs):
    """Harmless side-effect for the audit process."""
    instance.se_log = (instance.se_log or '') + 'audit_ok,'
    instance.save(update_fields=['se_log'])


# An independent process bound to Widget.audit_status. The unique constraint on
# TransitionMessage is per process, so an uncompleted row here must not block
# an uncompleted row on WidgetProcess for the same instance. It declares no
# queue, so it also covers the DJANGO_LOGIC['DEFAULT_QUEUE'] fallback.
class WidgetAuditProcess(Process):
    process_name = 'audit_process'
    transitions = [
        BackgroundTransition(
            action_name='audit',
            sources=['clean'],
            target='audited',
            in_progress_state='auditing',
            failed_state='audit_failed',
            side_effects=[bg_audit_ok],
        ),
    ]


# --- Filtered default manager ----------------------------------------------
# A model whose default manager hides archived rows. The worker must reload
# instances through _base_manager. Otherwise archiving an instance before the
# worker runs makes the reload raise DoesNotExist, the row completes, and the
# instance is stranded in its in_progress_state.


class ActiveOnlyManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(archived=False)


class ArchivableWidget(models.Model):
    status = models.CharField(max_length=32, default='draft')
    archived = models.BooleanField(default=False)

    # The filtered manager comes first, so it becomes _default_manager. With
    # no base_manager_name declared, _base_manager stays unfiltered.
    objects = ActiveOnlyManager()
    all_objects = models.Manager()

    class Meta:
        app_label = 'bg_tests'


def bg_noop(instance, **kwargs):
    pass


class ArchivableProcess(Process):
    process_name = 'process'
    transitions = [
        BackgroundTransition(
            action_name='finish',
            sources=['draft'],
            target='done',
            in_progress_state='finishing',
            failed_state='finish_failed',
            queue='django_logic.critical',
            side_effects=[bg_noop],
        ),
    ]


# --- Background transitions on nested processes -----------------------------
# Enqueue reaches a nested transition through the parent's
# get_available_transitions recursion, so the worker must also descend into
# nested_processes to restore it. These processes drive the same Widget.status
# field as WidgetProcess through a separate accessor (`parent_process`), so no
# migration is needed.


class NestedBgGrandchildProcess(Process):
    """Two levels deep, so the search must recurse and not stop at one hop."""

    process_name = 'nested_grandchild'
    transitions = [
        BackgroundTransition(
            action_name='deeply_nested_fulfil',
            sources=['draft'],
            target='deeply_nested_fulfilled',
            in_progress_state='deeply_nested_fulfilling',
            failed_state='deeply_nested_failed',
            queue='django_logic.critical',
            side_effects=[bg_ok],
            callbacks=[bg_callback],
        ),
    ]


class NestedBgMidProcess(Process):
    """Middle layer: carries no transitions of its own, only a nested child."""

    process_name = 'nested_mid'
    nested_processes = [NestedBgGrandchildProcess]


class NestedBgChildProcess(Process):
    """A nested process that owns background transitions. Callers reach it only
    through its parent's ``nested_processes``; it is never bound directly."""

    process_name = 'nested_child'
    transitions = [
        BackgroundTransition(
            action_name='nested_fulfil',
            sources=['draft'],
            target='nested_fulfilled',
            in_progress_state='nested_fulfilling',
            failed_state='nested_failed',
            queue='django_logic.critical',
            side_effects=[bg_ok, bg_record_kwargs],
            callbacks=[bg_callback],
            failure_callbacks=[bg_failure_callback],
        ),
        BackgroundAction(
            action_name='nested_sync_inventory',
            sources=['nested_fulfilled'],
            queue='django_logic.fast',
            side_effects=[bg_ok],
            callbacks=[bg_callback],
        ),
        BackgroundTransition(
            action_name='nested_crash',
            sources=['draft'],
            target='nested_crash_target',
            in_progress_state='nested_crashing',
            failed_state='nested_crash_failed',
            queue='django_logic.critical',
            side_effects=[bg_boom],
            failure_callbacks=[bg_failure_callback],
        ),
    ]


class WidgetParentProcess(Process):
    """Parent bound to Widget.status. It declares no background transitions of
    its own; they live on the nested processes it delegates to."""

    process_name = 'parent_process'
    nested_processes = [NestedBgChildProcess, NestedBgMidProcess]


# --- Nested background transitions chosen by a condition --------------------
# ConversationProcess routes per messaging integration through two nested
# processes, Gmail and Dummy. Both declare background transitions that share an
# action_name, and a condition on the instance picks one. Enqueue records the
# owning nested process class on the row, and the worker restores that exact
# transition without evaluating the condition again.


class Conversation(models.Model):
    status = models.CharField(max_length=32, default='open')
    # The field the nested processes' conditions read.
    source_integration = models.CharField(max_length=32, default='gmail')
    se_log = models.TextField(default='', blank=True)
    cb_log = models.TextField(default='', blank=True)

    class Meta:
        app_label = 'bg_tests'


def conv_is_gmail(instance, **kwargs):
    return instance.source_integration == 'gmail'


def conv_is_dummy(instance, **kwargs):
    return instance.source_integration == 'dummy'


def conv_send_gmail(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'gmail_send,'
    instance.save(update_fields=['se_log'])


def conv_send_dummy(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'dummy_send,'
    instance.save(update_fields=['se_log'])


class GmailConversationProcess(Process):
    """Nested process for one integration. Its transitions are chosen when the
    instance's ``source_integration`` is ``'gmail'``."""

    process_name = 'gmail_conversation'
    transitions = [
        BackgroundTransition(
            action_name='send_message_via_integration',
            sources=['open'],
            target='open',
            in_progress_state='gmail_sending',
            failed_state='gmail_send_failed',
            conditions=[conv_is_gmail],
            queue='django_logic.critical',
            side_effects=[conv_send_gmail],
            callbacks=[bg_callback],
        ),
        BackgroundTransition(
            action_name='close',
            sources=['open'],
            target='closed',
            in_progress_state='gmail_closing',
            failed_state='gmail_close_failed',
            conditions=[conv_is_gmail],
            queue='django_logic.critical',
            side_effects=[bg_noop],
        ),
    ]


class DummyConversationProcess(Process):
    process_name = 'dummy_conversation'
    transitions = [
        BackgroundTransition(
            action_name='send_message_via_integration',
            sources=['open'],
            target='open',
            in_progress_state='dummy_sending',
            failed_state='dummy_send_failed',
            conditions=[conv_is_dummy],
            queue='django_logic.critical',
            side_effects=[conv_send_dummy],
            callbacks=[bg_callback],
        ),
        BackgroundTransition(
            action_name='close',
            sources=['open'],
            target='closed',
            in_progress_state='dummy_closing',
            failed_state='dummy_close_failed',
            conditions=[conv_is_dummy],
            queue='django_logic.critical',
            side_effects=[bg_noop],
        ),
    ]


class ConversationProcess(Process):
    """Bound parent with no transitions of its own. A caller invokes
    ``conversation.process.send_message_via_integration(...)`` and the nested
    processes' conditions route the call to the right integration."""

    process_name = 'process'
    nested_processes = [GmailConversationProcess, DummyConversationProcess]


# Two nested background transitions that share an action_name and whose
# conditions both pass. The validator allows a shared background action_name
# across nested classes, so this misconfiguration is caught at enqueue time,
# just like duplicate synchronous action_names. These fixtures let a test pin
# that enqueue raises before it writes an in_progress_state or a row.


def conv_always(instance, **kwargs):
    return True


class AmbiguousAProcess(Process):
    process_name = 'ambig_a'
    transitions = [
        BackgroundTransition(
            action_name='ambiguous_send',
            sources=['open'],
            target='open',
            in_progress_state='ambig_a_sending',
            conditions=[conv_always],
            queue='django_logic.critical',
            side_effects=[bg_noop],
        ),
    ]


class AmbiguousBProcess(Process):
    process_name = 'ambig_b'
    transitions = [
        BackgroundTransition(
            action_name='ambiguous_send',
            sources=['open'],
            target='open',
            in_progress_state='ambig_b_sending',
            conditions=[conv_always],
            queue='django_logic.critical',
            side_effects=[bg_noop],
        ),
    ]


class AmbiguousConversationProcess(Process):
    process_name = 'ambiguous_process'
    nested_processes = [AmbiguousAProcess, AmbiguousBProcess]


# Two nested BackgroundActions that share an action_name, have identical
# sources, and declare no in_progress_state. The worker's state guard only
# checks that the current state is in sources, so it cannot tell them apart. A
# row with no recorded owner must not resolve to the first match here, or the
# wrong integration's side-effects run.


def conv_act_a(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'act_a,'
    instance.save(update_fields=['se_log'])


def conv_act_b(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'act_b,'
    instance.save(update_fields=['se_log'])


class SharedActionAProcess(Process):
    process_name = 'shared_act_a'
    transitions = [
        BackgroundAction(
            action_name='shared_sync',
            sources=['open'],
            conditions=[conv_is_gmail],
            queue='django_logic.fast',
            side_effects=[conv_act_a],
        ),
    ]


class SharedActionBProcess(Process):
    process_name = 'shared_act_b'
    transitions = [
        BackgroundAction(
            action_name='shared_sync',
            sources=['open'],
            conditions=[conv_is_dummy],
            queue='django_logic.fast',
            side_effects=[conv_act_b],
        ),
    ]


class SharedActionConversationProcess(Process):
    process_name = 'shared_action_process'
    nested_processes = [SharedActionAProcess, SharedActionBProcess]


# A synchronous transition and a background transition that share an
# action_name in one process, routed by a condition on the instance. The worker
# only looks at background transitions, so the synchronous namesake is
# invisible to it. 'archive' runs inline for gmail and durably for dummy.


def conv_sync_archive(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'sync_archive,'
    instance.save(update_fields=['se_log'])


def conv_bg_archive(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'bg_archive,'
    instance.save(update_fields=['se_log'])


class MixedSyncBgProcess(Process):
    process_name = 'mixed_process'
    transitions = [
        Transition(
            action_name='archive',
            sources=['open'],
            target='archived_sync',
            conditions=[conv_is_gmail],
            side_effects=[conv_sync_archive],
        ),
        BackgroundTransition(
            action_name='archive',
            sources=['open'],
            target='archived_bg',
            in_progress_state='archiving_bg',
            conditions=[conv_is_dummy],
            queue='django_logic.fast',
            side_effects=[conv_bg_archive],
        ),
    ]


# --- Processes attached to Widget for individual test modules ---------------
# They live here so every bind_model_process call for this app stays in
# apps.py. The test modules import these symbols.


# A minimal process with one condition and one permission, bound as `guard`.

def _stock_ok(instance):
    return getattr(instance, '_stock_available', True)


def _is_staff(instance, user):
    return bool(user and getattr(user, 'is_staff', False))


class ScenarioGuardProcess(Process):
    process_name = 'guard'
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            conditions=[_stock_ok],
            permissions=[_is_staff],
        ),
    ]


# `approve` chains into `notify` through next_transition. The follow-up's
# side-effect must be tracked even though the test only drives `approve`.
# ``RAN`` records the call order.

RAN: list = []


def chain_first(instance, **kwargs):
    RAN.append('chain_first')


def chain_followup(instance, **kwargs):
    RAN.append('chain_followup')


class WidgetChainProcess(Process):
    process_name = 'chain_process'
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            side_effects=[chain_first],
            next_transition='notify',
        ),
        Transition(
            action_name='notify',
            sources=['approved'],
            target='notified',
            side_effects=[chain_followup],
        ),
    ]


# --- Fixtures for ProcessScenario behaviour tests ---------------------------
# These processes cover the synchronous Transition and Action matrix and the
# background-to-background next_transition chain. Every side-effect appends a
# marker to se_log or cb_log, so a test asserts on how the object changed
# rather than on a return value.


def _se(marker):
    """Return a side-effect that appends ``marker`` to se_log. Its __name__ is
    stable so track() and assert_side_effects_ran can refer to it."""
    def fn(instance, **kwargs):
        instance.se_log = (instance.se_log or '') + marker + ','
        instance.save(update_fields=['se_log'])
    fn.__name__ = f'se_{marker}'
    return fn


def _cb(marker):
    """Return a callback that appends ``marker`` to cb_log."""
    def fn(instance, **kwargs):
        instance.cb_log = (instance.cb_log or '') + marker + ','
        instance.save(update_fields=['cb_log'])
    fn.__name__ = f'cb_{marker}'
    return fn


# Two things se_log and cb_log cannot record: the order of the failure hooks,
# and the kwargs a side-effect received.
SYNC_ORDER: list = []
SYNC_LAST_KWARGS: dict = {}


def _fcb(marker):
    """Return a failure callback that appends ``fcb_<marker>`` to cb_log and
    records its position in SYNC_ORDER."""
    def fn(instance, **kwargs):
        instance.cb_log = (instance.cb_log or '') + 'fcb_' + marker + ','
        instance.save(update_fields=['cb_log'])
        SYNC_ORDER.append(f'fcb:{marker}')
    fn.__name__ = f'fcb_{marker}'
    return fn


def sync_boom(instance, **kwargs):
    """Side-effect that always raises. Failure tests target it with
    fail_side_effect='sync_boom'."""
    raise ValueError('sync boom')


def sync_cb_boom(instance, **kwargs):
    """Callback that always raises, so a test can check that the engine
    swallows the exception and keeps the target state."""
    raise ValueError('callback boom')


def sync_capture(instance, **kwargs):
    """Side-effect that records its kwargs in SYNC_LAST_KWARGS, so a test can
    check kwargs forwarding and transition-context chaining."""
    instance.se_log = (instance.se_log or '') + 'captured,'
    instance.save(update_fields=['se_log'])
    SYNC_LAST_KWARGS.clear()
    SYNC_LAST_KWARGS.update(kwargs)


def sync_capture_fail(instance, exception, **kwargs):
    """Failure callback that records its kwargs and the exception, so a test
    can check that failure hooks receive both."""
    SYNC_LAST_KWARGS.clear()
    SYNC_LAST_KWARGS.update(kwargs)
    SYNC_LAST_KWARGS['exception'] = exception


# The state stored in the database at the moment each callback ran. The
# callback below reads a fresh row, so it sees the stored write and not an
# in-memory attribute.
CALLBACK_SEEN_STATE: list = []


def cb_record_seen_state(instance, **kwargs):
    from_db = type(instance).objects.get(pk=instance.pk)
    CALLBACK_SEEN_STATE.append(from_db.status)
    instance.cb_log = (instance.cb_log or '') + 'seen_state,'
    instance.save(update_fields=['cb_log'])


def _always(instance, **kwargs):
    return True


def _flagged(instance, **kwargs):
    return 'flag' in (instance.kwargs_seen or [])


def _not_flagged(instance, **kwargs):
    return 'flag' not in (instance.kwargs_seen or [])


def _is_staff_user(instance, user=None, **kwargs):
    return bool(user and getattr(user, 'is_staff', False))


class WidgetSyncProcess(Process):
    """The synchronous Transition and Action matrix on Widget.status, bound as
    ``sync_proc``.

    It covers ordered side-effects, next_transition chaining, the failure path,
    a synchronous Action, a swallowed callback exception, two transitions with
    one action_name split by a condition, a permission gate, and kwargs
    forwarding into the failure hooks.
    """

    process_name = 'sync_proc'
    transitions = [
        Transition('approve', sources=['draft'], target='approved',
                   side_effects=[_se('a'), _se('b')],
                   callbacks=[_cb('after_approve')],
                   next_transition='notify'),
        Transition('notify', sources=['approved'], target='notified',
                   side_effects=[_se('c')]),
        Transition('reject', sources=['draft'], target='rejected',
                   failed_state='rejection_failed',
                   side_effects=[_se('reject_attempt')],
                   failure_callbacks=[_fcb('on_fail')]),
        # The target is written before callbacks run, so a raising callback
        # cannot undo it.
        Transition('boom_callback', sources=['draft'], target='boom_done',
                   callbacks=[sync_cb_boom]),
        Action('poke', sources=['draft'],
               side_effects=[_se('poke')],
               callbacks=[_cb('after_poke')]),
        Action('poke_fail', sources=['draft'], failed_state='poked_failed',
               side_effects=[_se('poke_attempt')],
               failure_callbacks=[_fcb('on_poke_fail')]),
        Transition('cancel', sources=['draft'], target='cancelled',
                   conditions=[_not_flagged],
                   side_effects=[_se('cancel_plain')]),
        Transition('cancel', sources=['draft'], target='archived',
                   conditions=[_flagged],
                   side_effects=[_se('cancel_flagged')]),
        Transition('staff_only', sources=['draft'], target='staffed',
                   permissions=[_is_staff_user],
                   side_effects=[_se('staff')]),
        # kwargs forwarding, and what the failure hooks receive.
        Transition('capture', sources=['draft'], target='captured',
                   side_effects=[sync_capture]),
        Transition('capture_fail', sources=['draft'], target='captured',
                   failed_state='capture_failed',
                   side_effects=[sync_boom],
                   failure_callbacks=[sync_capture_fail]),
        # The target is stored before callbacks run.
        Transition('finalize', sources=['draft'], target='finalized',
                   side_effects=[_se('finalize')],
                   callbacks=[cb_record_seen_state]),
    ]


class WidgetContextProcess(Process):
    """A two-step synchronous chain on Widget.status, bound as ``ctx_proc``. It
    lets a test check that next_transition gives the follow-up a new tr_id and
    carries root_id and parent_id across. The follow-up records its kwargs in
    SYNC_LAST_KWARGS."""

    process_name = 'ctx_proc'
    transitions = [
        Transition('parent_act', sources=['draft'], target='parent_done',
                   side_effects=[_se('parent')], next_transition='child_act'),
        Transition('child_act', sources=['parent_done'], target='child_done',
                   side_effects=[sync_capture]),
    ]


class InnerSyncProcess(Process):
    """Nested process that owns a synchronous transition, reached only through
    its parent's ``nested_processes``."""

    process_name = 'inner_sync'
    transitions = [
        Transition('inner_act', sources=['draft'], target='inner_done',
                   side_effects=[_se('inner')]),
    ]


class WidgetNestedSyncProcess(Process):
    """Parent (``nested_sync``) delegating to :class:`InnerSyncProcess`."""

    process_name = 'nested_sync'
    nested_processes = [InnerSyncProcess]


class WidgetAmbiguousNextProcess(Process):
    """``start`` chains into ``follow``, but two ``follow`` transitions are
    available from ``started`` and no condition separates them. The engine must
    refuse the follow-up and run neither, instead of picking one."""

    process_name = 'ambig_next'
    transitions = [
        Transition('start', sources=['draft'], target='started',
                   side_effects=[_se('start')], next_transition='follow'),
        Transition('follow', sources=['started'], target='a_done',
                   side_effects=[_se('follow_a')]),
        Transition('follow', sources=['started'], target='b_done',
                   side_effects=[_se('follow_b')]),
    ]


class WidgetBgChainProcess(Process):
    """A background-to-background next_transition chain on Widget.status,
    bound as ``bg_chain``.

    ``bg_fulfil`` chains into ``bg_export``. The follow-up row must record its
    own owner and not the first transition's, and the widget must pass through
    every state on the way: chain_fulfilling, fulfilled, chain_exporting,
    exported.
    """

    process_name = 'bg_chain'
    transitions = [
        BackgroundTransition('bg_fulfil', sources=['draft'], target='fulfilled',
                             in_progress_state='chain_fulfilling',
                             failed_state='chain_fulfil_failed',
                             queue='django_logic.critical',
                             side_effects=[_se('bg_fulfil_se')],
                             next_transition='bg_export'),
        BackgroundTransition('bg_export', sources=['fulfilled'], target='exported',
                             in_progress_state='chain_exporting',
                             failed_state='chain_export_failed',
                             queue='django_logic.slow',
                             side_effects=[_se('bg_export_se')],
                             callbacks=[_cb('bg_export_cb')]),
    ]


# A nested background chain on Conversation, split by condition. Each
# integration owns a background ``send`` (open -> open) that chains into a
# background ``report`` (open -> reported). The follow-up row must record the
# nested class that owns it, not the bound parent and not the first transition.


def chain_is_gmail(instance, **kwargs):
    return instance.source_integration == 'gmail'


def chain_is_dummy(instance, **kwargs):
    return instance.source_integration == 'dummy'


def chain_gmail_send(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'gmail_send,'
    instance.save(update_fields=['se_log'])


def chain_gmail_report(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'gmail_report,'
    instance.save(update_fields=['se_log'])


def chain_dummy_send(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'dummy_send,'
    instance.save(update_fields=['se_log'])


def chain_dummy_report(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'dummy_report,'
    instance.save(update_fields=['se_log'])


class GmailChainProcess(Process):
    process_name = 'gmail_chain'
    transitions = [
        BackgroundTransition('send', sources=['open'], target='open',
                             in_progress_state='gmail_chain_sending',
                             failed_state='gmail_chain_send_failed',
                             conditions=[chain_is_gmail],
                             queue='django_logic.critical',
                             side_effects=[chain_gmail_send],
                             next_transition='report'),
        BackgroundTransition('report', sources=['open'], target='reported',
                             in_progress_state='gmail_chain_reporting',
                             failed_state='gmail_chain_report_failed',
                             conditions=[chain_is_gmail],
                             queue='django_logic.slow',
                             side_effects=[chain_gmail_report]),
    ]


class DummyChainProcess(Process):
    process_name = 'dummy_chain'
    transitions = [
        BackgroundTransition('send', sources=['open'], target='open',
                             in_progress_state='dummy_chain_sending',
                             failed_state='dummy_chain_send_failed',
                             conditions=[chain_is_dummy],
                             queue='django_logic.critical',
                             side_effects=[chain_dummy_send],
                             next_transition='report'),
        BackgroundTransition('report', sources=['open'], target='reported',
                             in_progress_state='dummy_chain_reporting',
                             failed_state='dummy_chain_report_failed',
                             conditions=[chain_is_dummy],
                             queue='django_logic.slow',
                             side_effects=[chain_dummy_report]),
    ]


class ChainConversationProcess(Process):
    """Bound parent (``chain_conv``) that delegates to one nested chain process
    per integration. A caller invokes ``conversation.chain_conv.send()`` and the
    conditions route it to the right chain."""

    process_name = 'chain_conv'
    nested_processes = [GmailChainProcess, DummyChainProcess]


# --- Two transitions with one action_name and conditions that both pass ------
# The engine must refuse and raise TransitionNotAllowed rather than pick one,
# with no state write and no side-effect.


class WidgetAmbiguousConditionProcess(Process):
    process_name = 'ambig_cond'
    transitions = [
        Transition('clash', sources=['draft'], target='clash_a',
                   conditions=[_always], side_effects=[_se('clash_a')]),
        Transition('clash', sources=['draft'], target='clash_b',
                   conditions=[_always], side_effects=[_se('clash_b')]),
    ]


# --- Conditions and permissions declared on the process ----------------------
# A condition or permission on the process class gates the whole process: its
# own transitions and every nested process's transitions. The engine skips the
# whole subtree when the process is not valid for the caller.


def process_gate_open(instance, **kwargs):
    """Condition on the process class. Nothing in the process is available
    unless the instance carries the 'gate_open' flag."""
    return 'gate_open' in (instance.kwargs_seen or [])


def process_requires_staff(instance, user=None, **kwargs):
    """Permission on the process class. No transition is available without a
    staff user. The engine only enforces a permission when the caller supplies
    a user, so user=None means there is no user context."""
    return bool(user and getattr(user, 'is_staff', False))


class GuardedInnerProcess(Process):
    """Nested process with no guards of its own. Callers reach it only through
    the guarded parent, so the parent's guards are what gate it."""

    process_name = 'guarded_inner'
    transitions = [
        Transition('inner_go', sources=['draft'], target='inner_gone',
                   side_effects=[_se('inner_go')]),
    ]


class WidgetProcGuardProcess(Process):
    """Bound as ``proc_guard``. Its ``conditions`` and ``permissions`` gate both
    ``go`` and the nested ``inner_go``."""

    process_name = 'proc_guard'
    conditions = [process_gate_open]
    permissions = [process_requires_staff]
    transitions = [
        Transition('go', sources=['draft'], target='gone',
                   side_effects=[_se('go')]),
    ]
    nested_processes = [GuardedInnerProcess]


# --- Failure that cascades across two state machines -------------------------
# These fixtures pin the anti-pattern so no engine change can alter it quietly.
# An outer transition's side-effect drives a transition on a different instance
# and lets the inner failure propagate. The result: the inner instance lands in
# its failed_state and runs its failure hooks, the exception propagates, the
# outer transition lands in its own failed_state and runs its failure hooks, the
# outer side-effects declared after the nested call are skipped, the outer
# success callbacks are skipped, and the exception reaches the outer caller.

CASCADE_ORDER: list = []


def cascade_inner_boom(instance, **kwargs):
    CASCADE_ORDER.append('inner:side_effect')
    raise ValueError('inner machine failed')


def cascade_inner_fcb(instance, exception=None, **kwargs):
    CASCADE_ORDER.append('inner:failure_callback')
    instance.cb_log = (instance.cb_log or '') + 'inner_fcb,'
    instance.save(update_fields=['cb_log'])


class CascadeInnerProcess(Process):
    process_name = 'cascade_inner'
    transitions = [
        Transition('inner_fulfil', sources=['draft'], target='inner_done',
                   failed_state='inner_failed',
                   side_effects=[cascade_inner_boom],
                   failure_callbacks=[cascade_inner_fcb]),
    ]


def cascade_outer_before(instance, **kwargs):
    CASCADE_ORDER.append('outer:before')
    instance.se_log = (instance.se_log or '') + 'outer_before,'
    instance.save(update_fields=['se_log'])


def cascade_call_inner(instance, inner_pk=None, **kwargs):
    """The anti-pattern: a side-effect drives another machine's transition and
    lets its exception propagate. Never re-raise a child error into a parent."""
    CASCADE_ORDER.append('outer:call_inner')
    Widget.objects.get(pk=inner_pk).cascade_inner.inner_fulfil()


def cascade_outer_after(instance, **kwargs):
    # Declared after the nested call, so it must be skipped once that raises.
    CASCADE_ORDER.append('outer:after')
    instance.se_log = (instance.se_log or '') + 'outer_after,'
    instance.save(update_fields=['se_log'])


def cascade_outer_cb(instance, **kwargs):
    # A success callback must not run when the transition fails.
    CASCADE_ORDER.append('outer:success_callback')
    instance.cb_log = (instance.cb_log or '') + 'outer_cb,'
    instance.save(update_fields=['cb_log'])


def cascade_outer_fcb(instance, exception=None, **kwargs):
    CASCADE_ORDER.append('outer:failure_callback')
    instance.cb_log = (instance.cb_log or '') + 'outer_fcb,'
    instance.save(update_fields=['cb_log'])


class CascadeOuterProcess(Process):
    process_name = 'cascade_outer'
    transitions = [
        Transition('outer_fulfil', sources=['draft'], target='outer_done',
                   failed_state='outer_failed',
                   side_effects=[cascade_outer_before, cascade_call_inner,
                                 cascade_outer_after],
                   callbacks=[cascade_outer_cb],
                   failure_callbacks=[cascade_outer_fcb]),
    ]
