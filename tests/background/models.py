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
