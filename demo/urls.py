from rest_framework import routers

from demo.viewsets import LockerViewSet

app_name = 'api'

router = routers.DefaultRouter()
router.register(r'locks', LockerViewSet, basename='lock')
urlpatterns = router.urls