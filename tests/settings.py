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
#   * Default: SQLite. The sync-mode suite passes here because sync mode
#     executes the transition inline and never runs
#     ``select_for_update(nowait=True)`` against real concurrency.
#   * Set ``POSTGRES_HOST`` (plus optional ``POSTGRES_{DB,USER,PASSWORD,PORT}``)
#     to run against PostgreSQL. Use it for the stability suite, which starts
#     real concurrent transactions and needs row locking.
#
# Celery mode rejects SQLite when it validates settings on startup, so a
# production misconfiguration fails immediately.
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

# A second alias pins the instance-alias routing: get_persisted_state and the
# engine's savepoints must follow instance._state.db, not 'default'. There is
# no global router here — tests/test_multidb_alias.py installs the routed
# topology per test, so the startup system checks see the default setup.
DATABASES['other'] = {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': ':memory:',
}

# LocMemCache by default. Tests that need an atomic add() use the
# @requires_real_redis skip decorator and run under settings_redis.py or
# settings_stability.py in dedicated CI jobs.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'django_logic',
    }
}

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

from django_logic.conf import enable_sync

# Sync is a test runtime, so boot refuses it unless a test settings
# module says so. This is the only place the suite opts in.
enable_sync()

DJANGO_LOGIC = {
    'LOCK_TIMEOUT': 7200,
    'BACKGROUND_EXECUTION': 'sync',
    'TRANSITION_MESSAGE_MAX_ERRORS': 5,
    'TRANSITION_MESSAGE_RETRY_MINUTES': 2,
    'TRANSITION_MESSAGE_CLEANUP_DAYS': 7,
}
