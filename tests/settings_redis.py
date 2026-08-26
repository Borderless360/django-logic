"""
Django settings for running stability tests with real Redis but SQLite DB.

Real Redis exercises the cache-backed lock against a real backend (atomic
add, TTL expiry) while keeping SQLite for simplicity. Use settings_stability
for the full Postgres+Redis environment.
"""
import os

from tests.settings import *

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ.get('REDIS_URL', 'redis://localhost:6379/1'),
    }
}

