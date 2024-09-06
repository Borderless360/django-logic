from django.urls import path, include


urlpatterns = [
    path('', include('demo.urls', namespace='demo')),
    path(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework')),
]