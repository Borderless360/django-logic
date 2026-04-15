from django.urls import path, include

urlpatterns = []

try:
    from demo import urls as demo_urls
    urlpatterns += [
        path('', include('demo.urls', namespace='demo')),
        path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    ]
except ImportError:
    pass