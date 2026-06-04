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
