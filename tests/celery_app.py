try:
    from celery import Celery
except ImportError:
    pass # TODO: handle it
else:
    app = Celery('tests')
    app.config_from_object('django.conf:settings', namespace='CELERY')
    app.autodiscover_tasks()
