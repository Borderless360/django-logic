"""
Django settings for stability tests.

Requires PostgreSQL and Redis (vs SQLite + LocMemCache for unit tests).
These are needed for:
  - select_for_update (not supported by SQLite)
  - Proper transaction isolation (SQLite serializes all writes)
  - Atomic cache.add with nx=True (LocMemCache is single-process only)
  - UniqueConstraint with condition (partial indexes, not supported by SQLite)
"""
import os

from tests.settings import *

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'django_logic_test'),
        'USER': os.environ.get('POSTGRES_USER', ''),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'OPTIONS': {
            'connect_timeout': 5,
        },
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': True,
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ.get('REDIS_URL', 'redis://localhost:6379/1'),
    }
}

