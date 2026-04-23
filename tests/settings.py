import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = 'django_logic'

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django_logic',
    'django_logic.background',
    'tests',
    'tests.stability',
    'tests.background',
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

ROOT_URLCONF = 'tests.urls'

MIDDLEWARE: list = []

# Database selection:
#   * Default: SQLite. The full sync-mode test suite passes here because
#     sync mode executes phase 2 inline and never exercises
#     ``select_for_update(nowait=True)`` against real concurrency.
#   * Set ``POSTGRES_HOST`` (plus optional ``POSTGRES_{DB,USER,PASSWORD,PORT}``)
#     to run against PostgreSQL. Recommended for the stability suite, which
#     spawns real concurrent transactions and needs proper row locking.
#
# Celery mode additionally rejects SQLite at ``validate_on_ready`` time
# (see ``django_logic.background.settings._reject_sqlite_in_celery_mode``)
# so production misconfigurations surface immediately.
if os.environ.get('POSTGRES_HOST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('POSTGRES_DB', 'django_logic_test'),
            'USER': os.environ.get('POSTGRES_USER', ''),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ['POSTGRES_HOST'],
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            'OPTIONS': {
                'connect_timeout': 5,
            },
            'CONN_MAX_AGE': 0,
            'CONN_HEALTH_CHECKS': True,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
        }
    }

# LocMemCache by default. Tests that need atomic nx semantics use the
# @requires_real_redis skip decorator and run under settings_redis.py
# / settings_stability.py in dedicated CI jobs.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'django_logic',
    }
}

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'STARTER_QUEUE': 'django_logic.starter',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
