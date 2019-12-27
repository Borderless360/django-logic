from celery import shared_task
import time
from demo.models import Invoice


@shared_task(acks_late=True)
def send_to_a_customer(*args, **kwargs):
    """
    It sends an invoice to the customer
    """
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    invoice.customer_received = True
    invoice.save(update_fields=['customer_received'])


@shared_task(acks_late=True)
def demo_task_1(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    time.sleep(5)
    print('TASK 1, Invoice status', invoice.status, args, kwargs)


@shared_task(acks_late=True)
def demo_task_2(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    time.sleep(5)
    print('TASK 2, Invoice status', invoice.status, args, kwargs)


@shared_task(acks_late=True)
def demo_task_3(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    time.sleep(5)
    print('TASK 3, Invoice status', invoice.status, args, kwargs)


@shared_task(acks_late=True)
def demo_task_4(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    time.sleep(5)
    print('TASK 4, Invoice status', invoice.status, args, kwargs)


@shared_task(acks_late=True)
def demo_task_5(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    time.sleep(5)
    print('TASK 5, Invoice status', invoice.status, args, kwargs)


@shared_task(acks_late=True)
def demo_task_exception(*args, **kwargs):
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    print('EXCEPTION TASK', invoice.status, args, kwargs)
    raise Exception
