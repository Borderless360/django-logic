from celery import shared_task

from demo.models import Invoice


@shared_task(acks_late=True)
def send_to_a_customer(*args, **kwargs):
    """
    It sends an invoice to the customer
    """
    invoice = Invoice.objects.get(pk=kwargs['instance_id'])
    invoice.customer_received = True
    invoice.save(update_fields=['customer_received'])
