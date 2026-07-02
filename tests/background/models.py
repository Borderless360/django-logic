"""Models + processes for background-transition tests.

Process↔model binding for this app happens in one place only —
``tests/background/apps.py`` (``BackgroundTestsConfig.ready()``). Binding at
module import time here would re-create the model→process→actions→model
circular import (issue #100); ``ready()`` runs after every app's models are
loaded, so it is the single supported binding site.
"""
from django.db import models

from django_logic import Action, Process, Transition
from django_logic.background import BackgroundAction, BackgroundTransition


def bg_ok(instance, **kwargs):
    instance.se_log = (instance.se_log or '') + 'ok,'
    instance.save(update_fields=['se_log'])


def bg_boom(instance, **kwargs):
    raise ValueError('boom')


# Captures the exact kwargs (values + types) a phase-2 side-effect received,
# so round-trip tests can assert on what crossed the phase-1/phase-2 boundary
# (user restoration, datetime->str, context presence) without a DB column.
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


def bg_fse_boom(instance, **kwargs):
    """Failure-side-effect that itself raises — used to test that the
    swallowed exception is recorded on the TransitionMessage."""
    raise RuntimeError('cleanup exploded')


class Widget(models.Model):
    status = models.CharField(max_length=32, default='draft')
    # R5: a SECOND state field driven by an independent process
    # (WidgetAuditProcess below). Two state machines on the same row must
    # be able to have background work in flight at the same time.
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
            action_name='crash_with_bad_cleanup',
            sources=['draft'],
            target='cwbc_target',
            in_progress_state='cwbc_in_progress',
            failed_state='cwbc_failed',
            queue='django_logic.critical',
            side_effects=[bg_boom],
            failure_side_effects=[bg_fse_boom],
        ),
        BackgroundTransition(
            action_name='timeboxed',
            sources=['draft'],
            target='tb_done',
            in_progress_state='tb_running',
            failed_state='tb_failed',
            queue='django_logic.slow',
            side_effects=[bg_ok],
            timeout=60,  # watchdog kicks in after 60s
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
    """Harmless side-effect for the audit process (R5 fixtures)."""
    instance.se_log = (instance.se_log or '') + 'audit_ok,'
    instance.save(update_fields=['se_log'])


# R5: an INDEPENDENT process bound to a different state field
# (Widget.audit_status). The per-process partial unique constraint on
# TransitionMessage means in-flight work here must not conflict with
# in-flight work on WidgetProcess ('process') for the same instance.
# Deliberately declares NO queue= so it exercises the
# DJANGO_LOGIC['DEFAULT_QUEUE'] fallback ('django_logic').
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


# --- Filtered-default-manager fixtures (issue #90) -------------------------
# A model whose default manager hides archived rows. The background runner
# must reload instances via _base_manager, or archiving an instance between
# phase 1 and phase 2 makes the restore raise DoesNotExist and the message
# is marked completed with the instance stranded in in_progress_state.


class ActiveOnlyManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(archived=False)


class ArchivableWidget(models.Model):
    status = models.CharField(max_length=32, default='draft')
    archived = models.BooleanField(default=False)

    # Filtered manager first = _default_manager. Django's _base_manager
    # stays a plain unfiltered Manager (no base_manager_name declared).
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


# --- Nested-process background transitions ---------------------------------
# Regression fixtures for the phase-2 restore of a BackgroundTransition that
# lives on a *nested* process. Phase 1 reaches it through the parent's
# get_available_transitions recursion; phase 2 must descend into
# nested_processes (runner._find_transition) to restore it. These processes
# operate on the same Widget.status field as WidgetProcess but are reached via
# a separate bound property (`parent_process`), so no migration is needed.


class NestedBgGrandchildProcess(Process):
    """Two levels deep — proves _find_transition recurses, not just one hop."""

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
    """A nested sub-process that owns durable background transitions. Reached
    only through its parent's ``nested_processes`` — never bound directly."""

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
    """Parent bound to Widget.status. Declares no background transitions of
    its own — they live on the nested processes it delegates to."""

    process_name = 'parent_process'
    nested_processes = [NestedBgChildProcess, NestedBgMidProcess]


# --- Condition-disambiguated nested background transitions (issue #98) ------
# A ConversationProcess routes per messaging integration via two nested
# processes (Gmail/Dummy), each declaring background transitions that SHARE an
# action_name, selected by a condition on the instance (source_integration).
# This is exactly the polymorphism the synchronous path always supported;
# issue #98 makes it work for durable background transitions too: phase 1
# records the owning nested process class on the TransitionMessage, and phase 2
# restores that exact transition without re-evaluating the condition.


class Conversation(models.Model):
    status = models.CharField(max_length=32, default='open')
    # The discriminator the nested processes' conditions key on.
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
    """Per-integration nested process; its transitions are selected when the
    instance's ``source_integration == 'gmail'``."""

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
    """Bound parent. Declares no transitions of its own — generic callers just
    invoke ``conversation.process.send_message_via_integration(...)`` and the
    nested processes' conditions route to the right integration."""

    process_name = 'process'
    nested_processes = [GmailConversationProcess, DummyConversationProcess]


# Overlapping-condition sibling background transitions (issue #98). The relaxed
# validator allows a shared background action_name across distinct nested
# classes regardless of whether the conditions are mutually exclusive — so a
# misconfiguration with overlapping conditions is caught at RUNTIME (phase 1),
# not at class creation, exactly like duplicate synchronous action_names. These
# fixtures let a test pin that the phase-1 ambiguity raises cleanly, before any
# in_progress_state write or TransitionMessage row.


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


# Two nested BackgroundActions that SHARE an action_name with IDENTICAL sources
# and no in_progress_state — the worst-case for an owner-less restore (issue #98
# review finding): the phase-2 state guard checks only ``current in sources`` and
# so cannot tell the siblings apart. A row that lost its owner must NOT be
# resolved by first-match here, or the wrong integration's side-effects fire.


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


# A synchronous transition and a background transition SHARING an action_name in
# one process, routed by a condition on the instance (issue #98: this is allowed
# now that phase 2 filters to is_background — the sync namesake is invisible to
# restore). 'archive' runs inline for gmail and durably for dummy.


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


# --- Test-local processes attached to Widget -------------------------------
# These were previously defined and bound inside their test modules. They live
# here so that every bind_model_process call for this app is centralised in
# apps.py (the single binding site); the tests import these symbols.


# Used by tests/test_scenario.py::GuardedApprovalScenario — a minimal process
# with a condition + permission, bound under the `guard` process name.

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


# Used by tests/test_issue_fixes_testing.py (#96) — `approve` chains into
# `notify` via next_transition; the follow-up's side-effect must be tracked
# even though only `approve` was driven. ``RAN`` records call order for the
# test's assertions.

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


# --- Scenario behavior fixtures (behavior-focused process tests) ----------
# Processes that exercise the sync Transition/Action matrix and the
# background->background next_transition chain, driven through the real
# Process entrypoint by ProcessScenario tests. Observables are se_log/cb_log
# (appended markers) so tests assert on how the OBJECT changes, not on
# framework return values. Bound in tests/background/apps.py (the single
# binding site) under distinct process_names on Widget/Conversation.


def _se(marker):
    """Side-effect factory: append ``marker`` to se_log. __name__ is stable
    so track()/assert_side_effects_ran can name it."""
    def fn(instance, **kwargs):
        instance.se_log = (instance.se_log or '') + marker + ','
        instance.save(update_fields=['se_log'])
    fn.__name__ = f'se_{marker}'
    return fn


def _cb(marker):
    """Callback factory: append ``marker`` to cb_log."""
    def fn(instance, **kwargs):
        instance.cb_log = (instance.cb_log or '') + marker + ','
        instance.save(update_fields=['cb_log'])
    fn.__name__ = f'cb_{marker}'
    return fn


# Module-global observables for behaviors that se_log/cb_log can't express:
# failure-hook ordering across the fse/fcb boundary, and kwargs capture.
SYNC_ORDER: list = []
SYNC_LAST_KWARGS: dict = {}


def _fse(marker):
    """Failure-side-effect: append ``fse_<marker>`` to se_log AND record the
    cross-hook ordering in SYNC_ORDER (so a test can assert fse runs before
    fcb)."""
    def fn(instance, **kwargs):
        instance.se_log = (instance.se_log or '') + 'fse_' + marker + ','
        instance.save(update_fields=['se_log'])
        SYNC_ORDER.append(f'fse:{marker}')
    fn.__name__ = f'fse_{marker}'
    return fn


def _fcb(marker):
    """Failure-callback: append ``fcb_<marker>`` to cb_log AND record the
    cross-hook ordering in SYNC_ORDER."""
    def fn(instance, **kwargs):
        instance.cb_log = (instance.cb_log or '') + 'fcb_' + marker + ','
        instance.save(update_fields=['cb_log'])
        SYNC_ORDER.append(f'fcb:{marker}')
    fn.__name__ = f'fcb_{marker}'
    return fn


def sync_boom(instance, **kwargs):
    """Side-effect that always raises — the injection target for failure
    tests (fail_side_effect='sync_boom')."""
    raise ValueError('sync boom')


def sync_cb_boom(instance, **kwargs):
    """Callback that always raises — proves a callback exception is
    swallowed (best-effort) and the target state is kept."""
    raise ValueError('callback boom')


def sync_capture(instance, **kwargs):
    """Side-effect that records the kwargs it received into SYNC_LAST_KWARGS
    so a test can assert on kwargs forwarding / transition-context chaining."""
    instance.se_log = (instance.se_log or '') + 'captured,'
    instance.save(update_fields=['se_log'])
    SYNC_LAST_KWARGS.clear()
    SYNC_LAST_KWARGS.update(kwargs)


def sync_capture_fail(instance, exception, **kwargs):
    """Failure-callback that records kwargs + the exception, so a test can
    assert failure hooks receive the ``exception`` kwarg and forwarded args."""
    SYNC_LAST_KWARGS.clear()
    SYNC_LAST_KWARGS.update(kwargs)
    SYNC_LAST_KWARGS['exception'] = exception


# Separate sink for the failure-SIDE-EFFECT exception/kwarg contract, so it is
# asserted independently of the failure-CALLBACK sink above.
SYNC_FSE_KWARGS: dict = {}


def sync_capture_fse(instance, exception=None, **kwargs):
    """Failure-side-effect that records the ``exception`` kwarg + forwarded
    caller kwargs. Pins that failure_side_effects (not just failure_callbacks)
    receive the original exception and the caller's kwargs."""
    SYNC_FSE_KWARGS.clear()
    SYNC_FSE_KWARGS.update(kwargs)
    SYNC_FSE_KWARGS['exception'] = exception


def sync_fse_boom(instance, exception=None, **kwargs):
    """Failure-side-effect that itself raises. Pins that a raising
    failure_side_effect is swallowed (does not mask the ORIGINAL exception,
    which still re-raises to the caller)."""
    raise RuntimeError('sync cleanup exploded')


# Callback that records the persisted state visible AT CALLBACK TIME, proving
# the target is written before callbacks run. Reads a fresh row from the DB so
# it observes the persisted write, not an in-memory attribute.
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
    """Sync Transition/Action matrix on Widget.status, bound as ``sync_proc``.

    Covers: ordered side-effects + next_transition chaining, the failure
    path (failed_state + failure_side_effects + failure_callbacks), a sync
    Action (no state change on success; failed_state only when unlocked),
    a swallowed callback exception, same-action-name disambiguation by
    condition, a permission gate, and kwargs forwarding / failure-hook
    exception contract.
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
                   failure_side_effects=[_fse('cleanup')],
                   failure_callbacks=[_fcb('on_fail')]),
        # Target is written before callbacks run, so a raising callback is
        # swallowed and the target state survives.
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
        # kwargs forwarding + failure-hook exception contract.
        Transition('capture', sources=['draft'], target='captured',
                   side_effects=[sync_capture]),
        Transition('capture_fail', sources=['draft'], target='captured',
                   failed_state='capture_failed',
                   side_effects=[sync_boom],
                   failure_side_effects=[sync_capture_fse],
                   failure_callbacks=[sync_capture_fail]),
        # Callback ordering: the target is persisted BEFORE callbacks run.
        Transition('finalize', sources=['draft'], target='finalized',
                   side_effects=[_se('finalize')],
                   callbacks=[cb_record_seen_state]),
        # A raising failure_side_effect must be swallowed and must NOT mask the
        # ORIGINAL side-effect exception, which still re-raises to the caller.
        Transition('reject_bad_cleanup', sources=['draft'], target='rbc_done',
                   failed_state='rbc_failed',
                   side_effects=[sync_boom],
                   failure_side_effects=[sync_fse_boom],
                   failure_callbacks=[_fcb('rbc')]),
    ]


class WidgetContextProcess(Process):
    """Two-step sync chain on Widget.status, bound as ``ctx_proc``, for
    asserting next_transition mints a fresh tr_id and chains root_id /
    parent_id across the follow-up. The follow-up captures its kwargs into
    SYNC_LAST_KWARGS via sync_capture."""

    process_name = 'ctx_proc'
    transitions = [
        Transition('parent_act', sources=['draft'], target='parent_done',
                   side_effects=[_se('parent')], next_transition='child_act'),
        Transition('child_act', sources=['parent_done'], target='child_done',
                   side_effects=[sync_capture]),
    ]


class InnerSyncProcess(Process):
    """Nested sub-process owning a sync transition, reached only via its
    parent's ``nested_processes`` — proves the parent entrypoint drives a
    nested transition behaviorally."""

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
    """``start`` chains into ``follow``, but TWO ``follow`` transitions are
    available from ``started`` with no disambiguating condition. The
    follow-up must be REFUSED (neither runs) rather than picking one
    arbitrarily — the behavior the old mock-based next_transition test
    pinned, now expressed through the entrypoint on a real object."""

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
    """Background -> background next_transition chain on Widget.status,
    bound as ``bg_chain``.

    ``bg_fulfil`` completes into ``bg_export`` via next_transition. This is
    the regression fixture for the untested owner-overwrite path: the
    follow-up TransitionMessage must record its OWN owner, not the
    predecessor's, and the object must pass through every intermediate
    state (chain_fulfilling -> fulfilled -> chain_exporting -> exported).
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


# Nested condition-disambiguated background chain on Conversation. Each
# integration owns a bg ``send`` (open -> open) that chains into a bg
# ``report`` (open -> reported) via next_transition. The follow-up must
# record the NESTED owning class (Gmail/Dummy), not the bound parent and
# not the predecessor — the riskiest owner-overwrite case for issue #98.


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
    """Bound parent (``chain_conv``) delegating to per-integration nested
    bg chain processes. Generic callers invoke ``conversation.chain_conv.send()``
    and conditions route to the right integration's chain."""

    process_name = 'chain_conv'
    nested_processes = [GmailChainProcess, DummyChainProcess]


# --- Ambiguous same-action-name transitions with OVERLAPPING conditions -----
# Two synchronous 'clash' transitions whose conditions BOTH pass. The resolve
# step must refuse (raise TransitionNotAllowed) rather than pick one — with no
# state write and no side-effect. Pins the resolve-time ambiguity contract that
# the previous test only claimed to (it never actually created ambiguity).


class WidgetAmbiguousConditionProcess(Process):
    process_name = 'ambig_cond'
    transitions = [
        Transition('clash', sources=['draft'], target='clash_a',
                   conditions=[_always], side_effects=[_se('clash_a')]),
        Transition('clash', sources=['draft'], target='clash_b',
                   conditions=[_always], side_effects=[_se('clash_b')]),
    ]


# --- Process-level conditions & permissions (Process.is_valid) ---------------
# A process-level condition/permission gates the WHOLE process — its own
# transitions AND every nested process's transitions — because
# _iter_available_with_owner short-circuits the entire subtree when
# is_valid(user) is False. These fixtures restore the class-level guard
# coverage (and the nested-inheritance case) that the migration dropped.


def process_gate_open(instance, **kwargs):
    """Process-level condition: the process is inert unless the instance is
    flagged 'gate_open'. Gates the class's own transitions and its nested
    process's transitions alike."""
    return 'gate_open' in (instance.kwargs_seen or [])


def process_requires_staff(instance, user=None, **kwargs):
    """Process-level permission: no transition is available/allowed without a
    staff user. (Per the engine contract, a permission is only enforced when a
    user is supplied; user=None means 'no user context'.)"""
    return bool(user and getattr(user, 'is_staff', False))


class GuardedInnerProcess(Process):
    """Nested process with NO guards of its own — reached only through the
    guarded parent, so the parent's process-level guard is what gates it."""

    process_name = 'guarded_inner'
    transitions = [
        Transition('inner_go', sources=['draft'], target='inner_gone',
                   side_effects=[_se('inner_go')]),
    ]


class WidgetProcGuardProcess(Process):
    """Bound as ``proc_guard``. Class-level ``conditions`` + ``permissions``
    gate BOTH ``go`` and the nested ``inner_go``."""

    process_name = 'proc_guard'
    conditions = [process_gate_open]
    permissions = [process_requires_staff]
    transitions = [
        Transition('go', sources=['draft'], target='gone',
                   side_effects=[_se('go')]),
    ]
    nested_processes = [GuardedInnerProcess]


# --- Cross-machine failure cascade (fundamental problem.md §3) ---------------
# THE ANTI-PATTERN, pinned so an engine change can't silently alter it: an
# outer transition's side-effect drives a transition on a DIFFERENT instance
# (a second state machine) and lets that inner failure propagate. The cascade
# then is: inner lands in its failed_state with inner failure hooks run -> the
# exception propagates -> outer's fail_transition runs (outer -> its
# failed_state, outer failure hooks run) -> outer's side-effects declared AFTER
# the nested call are SKIPPED -> outer's success callbacks are SKIPPED -> the
# exception reaches the caller of the outer transition. This is exactly the
# 0.1.6->0.2.0 cascade; one journey test locks every leg of it.

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
    lets its exception propagate (never re-raise a child error into a parent —
    see CLAUDE.md rule 3). The inner pk is forwarded as a caller kwarg."""
    CASCADE_ORDER.append('outer:call_inner')
    Widget.objects.get(pk=inner_pk).cascade_inner.inner_fulfil()


def cascade_outer_after(instance, **kwargs):
    # Declared AFTER the nested call; must be SKIPPED once it raises.
    CASCADE_ORDER.append('outer:after')
    instance.se_log = (instance.se_log or '') + 'outer_after,'
    instance.save(update_fields=['se_log'])


def cascade_outer_cb(instance, **kwargs):
    # Success callback; must NOT run when the transition fails.
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
