"""Models + processes for background-transition tests."""
from django.db import models

from django_logic import Process, ProcessManager, Transition
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


ProcessManager.bind_model_process(Widget, WidgetProcess, state_field='status')


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


ProcessManager.bind_model_process(Widget, WidgetAuditProcess, state_field='audit_status')


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


ProcessManager.bind_model_process(ArchivableWidget, ArchivableProcess, state_field='status')


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


ProcessManager.bind_model_process(Widget, WidgetParentProcess, state_field='status')


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


ProcessManager.bind_model_process(Conversation, ConversationProcess, state_field='status')


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


ProcessManager.bind_model_process(
    Conversation, AmbiguousConversationProcess, state_field='status'
)
