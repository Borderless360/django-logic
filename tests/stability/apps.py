from django.apps import AppConfig

from django_logic import ProcessManager


class StabilityConfig(AppConfig):
    name = 'tests.stability'
    label = 'stability'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # The single binding site for this app. ready() runs after all models
        # are loaded, so binding here can never trigger the import cycle that
        # module-level binding in models.py would (issue #100). Keep the
        # model/process imports inside ready().
        from .models import (
            FulfillmentProcess,
            MultiProcessOrder,
            Order,
            OrderProcess,
            PaymentProcess,
        )

        ProcessManager.bind_model_process(Order, OrderProcess, state_field='status')
        ProcessManager.bind_model_process(
            MultiProcessOrder, FulfillmentProcess, state_field='fulfillment_status'
        )
        ProcessManager.bind_model_process(
            MultiProcessOrder, PaymentProcess, state_field='payment_status'
        )
