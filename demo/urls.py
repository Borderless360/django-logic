from rest_framework import routers

from demo.viewsets import InvoiceViewSet

app_name = 'api'

router = routers.DefaultRouter()
router.register(r'invoices', InvoiceViewSet, basename='invoice')
urlpatterns = router.urls