from rest_framework import serializers

from demo.models import Invoice


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ['id', 'status', 'customer_received', 'is_available']
        readonly = ['id', 'status']
