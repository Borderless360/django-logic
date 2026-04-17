"""
Models and processes used exclusively by stability tests.

These are intentionally simple -- the goal is to test the framework's
concurrency, crash recovery, and locking behavior, not business logic.
"""
from django.db import models

from django_logic import Process, Transition, Action, ProcessManager


# ---------------------------------------------------------------------------
# Side-effect functions (plain Python callables, as the framework requires)
# ---------------------------------------------------------------------------

def side_effect_one(instance, **kwargs):
    instance.side_effect_log = (instance.side_effect_log or '') + 'se1,'
    instance.save(update_fields=['side_effect_log'])


def side_effect_two(instance, **kwargs):
    instance.side_effect_log = (instance.side_effect_log or '') + 'se2,'
    instance.save(update_fields=['side_effect_log'])


def side_effect_three(instance, **kwargs):
    instance.side_effect_log = (instance.side_effect_log or '') + 'se3,'
    instance.save(update_fields=['side_effect_log'])


def side_effect_slow(instance, **kwargs):
    """Simulates a slow external API call."""
    import time
    time.sleep(kwargs.get('sleep_seconds', 0.1))
    instance.side_effect_log = (instance.side_effect_log or '') + 'slow,'
    instance.save(update_fields=['side_effect_log'])


def callback_one(instance, **kwargs):
    instance.callback_log = (instance.callback_log or '') + 'cb1,'
    instance.save(update_fields=['callback_log'])


def callback_two(instance, **kwargs):
    instance.callback_log = (instance.callback_log or '') + 'cb2,'
    instance.save(update_fields=['callback_log'])


def failure_side_effect(instance, **kwargs):
    instance.failure_log = (instance.failure_log or '') + 'fse,'
    instance.save(update_fields=['failure_log'])


def failure_callback(instance, **kwargs):
    instance.failure_log = (instance.failure_log or '') + 'fcb,'
    instance.save(update_fields=['failure_log'])


def trigger_nested_transition(instance, **kwargs):
    """Callback that triggers another transition on the SAME instance."""
    instance.process.complete()


def trigger_other_instance_transition(instance, **kwargs):
    """Callback that triggers a transition on a DIFFERENT instance."""
    other_id = kwargs.get('other_instance_id')
    if other_id:
        other = Order.objects.get(pk=other_id)
        other.process.ship()


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------

class Order(models.Model):
    status = models.CharField(max_length=32, default='draft')
    side_effect_log = models.TextField(default='', blank=True)
    callback_log = models.TextField(default='', blank=True)
    failure_log = models.TextField(default='', blank=True)

    class Meta:
        app_label = 'stability'


class MultiProcessOrder(models.Model):
    """Model with two independent processes on different state fields."""
    fulfillment_status = models.CharField(max_length=32, default='pending')
    payment_status = models.CharField(max_length=32, default='unpaid')
    side_effect_log = models.TextField(default='', blank=True)

    class Meta:
        app_label = 'stability'


# ---------------------------------------------------------------------------
# Process definitions
# ---------------------------------------------------------------------------

class OrderProcess(Process):
    process_name = 'process'
    transitions = [
        Transition(
            action_name='approve',
            sources=['draft'],
            target='approved',
            side_effects=[side_effect_one],
            callbacks=[callback_one],
        ),
        Transition(
            action_name='fulfill',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            failed_state='fulfillment_failed',
            side_effects=[side_effect_one, side_effect_two, side_effect_three],
            callbacks=[callback_one, callback_two],
            failure_side_effects=[failure_side_effect],
            failure_callbacks=[failure_callback],
        ),
        Transition(
            action_name='complete',
            sources=['fulfilled'],
            target='completed',
        ),
        Transition(
            action_name='ship',
            sources=['approved'],
            target='shipped',
            side_effects=[side_effect_one],
        ),
        Transition(
            action_name='cancel',
            sources=['draft', 'approved'],
            target='cancelled',
        ),
    ]


class OrderProcessWithNestedCallback(Process):
    """Process where a callback triggers another transition on the same instance."""
    process_name = 'process'
    transitions = [
        Transition(
            action_name='fulfill',
            sources=['approved'],
            target='fulfilled',
            in_progress_state='fulfilling',
            side_effects=[side_effect_one],
            callbacks=[trigger_nested_transition],
        ),
        Transition(
            action_name='complete',
            sources=['fulfilled'],
            target='completed',
        ),
    ]


class FulfillmentProcess(Process):
    process_name = 'fulfillment_process'
    transitions = [
        Transition(
            action_name='start_fulfillment',
            sources=['pending'],
            target='fulfilled',
            in_progress_state='fulfilling',
            side_effects=[side_effect_one],
        ),
    ]


class PaymentProcess(Process):
    process_name = 'payment_process'
    transitions = [
        Transition(
            action_name='pay',
            sources=['unpaid'],
            target='paid',
            in_progress_state='processing_payment',
            side_effects=[side_effect_two],
        ),
    ]


ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')
ProcessManager.bind_model_process(
    MultiProcessOrder, FulfillmentProcess, state_field='fulfillment_status'
)
ProcessManager.bind_model_process(
    MultiProcessOrder, PaymentProcess, state_field='payment_status'
)
