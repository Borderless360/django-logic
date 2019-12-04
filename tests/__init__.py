try:
    from .celery_app import app as celery_app
except ImportError:
    pass
else:
    __all__ = ('celery_app',)