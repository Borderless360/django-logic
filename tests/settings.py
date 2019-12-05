import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = 'django_logic'

PROJECT_APPS = [
    'django_logic',
    'demo',
]

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
] + PROJECT_APPS

try:
    import rest_framework
except ImportError:
    pass
else:
    INSTALLED_APPS += ['rest_framework']
    REST_FRAMEWORK = {
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.AllowAny'
        ]
    }
ROOT_URLCONF = 'tests.urls'

MIDDLEWARE = []

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
    }
}
CELERY_BROKER_URL = 'amqp://guest:guest@127.0.0.1:5672//'
# TODO: add warning to the doc
CELERY_TASK_SERIALIZER = 'pickle'
CELERY_ACCEPT_CONTENT = ['json', 'pickle', 'application/x-python-serialize']

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'django_logic',
    }
}

MIGRATION_MODULES = {
    'auth': None,
    'contenttypes': None,
}