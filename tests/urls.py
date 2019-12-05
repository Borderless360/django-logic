from django.conf.urls import url, include


urlpatterns = [
    url(r'^', include('demo.urls', namespace='demo')),
    url(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework'))
]