"""
Django settings for running stability tests with real Redis but SQLite DB.

This unlocks all RedisState tests (nx=True atomicity, lock expiry, etc.)
while keeping the SQLite database for simplicity. Use settings_stability
for the full Postgres+Redis environment.
"""
import os

from tests.settings import *  # noqa: F401, F403

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': os.environ.get('REDIS_URL', 'redis://localhost:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
    }
}

