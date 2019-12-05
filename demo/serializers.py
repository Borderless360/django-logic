from rest_framework import serializers

from demo.models import Invoice


class InvoiceSerializer(serializers.ModelSerializer):
    actions = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = ('id', 'status', 'customer_received', 'is_available', 'actions')
        readonly = ('id', 'status')

    def get_actions(self, instance):
        return sorted(set([transition.action_name for transition in
                           instance.invoice_process.get_available_transitions()]))
