from rest_framework import viewsets

from demo.models import Lock
from demo.serializers import LockerSerializer


class LockerViewSet(viewsets.ModelViewSet):
    queryset = Lock.objects.all()
    serializer_class = LockerSerializer
