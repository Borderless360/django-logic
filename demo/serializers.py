from rest_framework import serializers

from demo.models import Lock


class LockerSerializer(serializers.ModelSerializer):
    actions = serializers.SerializerMethodField()

    class Meta:
        model = Lock
        fields = ('id', 'actions', 'status')
        readonly = ('id', 'status')

    def get_actions(self, instance):
        return sorted(set([transition.action_name for transition in
                           instance.process.get_available_transitions()]))
