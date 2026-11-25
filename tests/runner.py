"""Run the full suite on Linux with PostgreSQL and Redis, or the supported local tests."""
from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache
from django.db import connection
from django.test.runner import DiscoverRunner


class FullTestRunner(DiscoverRunner):
    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)
        for test, reason in result.skipped:
            self.log(f'{test.id()} did not run: {reason}')
        if not result.testsRun:
            self.log('No tests ran. Check the test label and the required database and cache.')
        return failures + len(result.skipped) + len(result.expectedFailures) + int(not result.testsRun)


class LocalTestRunner(FullTestRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if connection.vendor != 'postgresql':
            self.exclude_tags.add('postgresql')
        if not isinstance(caches['default'], RedisCache):
            self.exclude_tags.add('redis')
        if self.exclude_tags:
            self.log('Excluded test tags: ' + ', '.join(sorted(self.exclude_tags)))
