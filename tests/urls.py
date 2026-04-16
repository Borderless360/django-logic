from django.urls import path, include


urlpatterns = [
    path('', include('demo.urls', namespace='demo')),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
]