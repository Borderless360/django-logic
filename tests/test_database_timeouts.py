"""Timeout limits preserve stricter settings and end with their transaction."""
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from django_logic.background import fairness, pull
from django_logic.background.models import TransitionMessage
from django_logic.database import set_local_timeouts
from django_logic.lock_ownership import _lock_connection
from tests.stability.base import requires_postgres


@requires_postgres
class DatabaseTimeoutTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
            )
            original = cursor.fetchone()
            self.addCleanup(self.restore_settings, *original)
            self.set_settings(cursor, '0', '0', local=False)

    @staticmethod
    def set_settings(cursor, statement, lock, *, local=True):
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, %s), "
            "set_config('lock_timeout', %s, %s)",
            [statement, local, lock, local],
        )

    def restore_settings(self, statement, lock):
        with connection.cursor() as cursor:
            self.set_settings(cursor, statement, lock, local=False)

    @staticmethod
    def read_timeouts(cursor):
        # PostgreSQL exposes these numeric values in milliseconds, independently
        # of the unit conversion used by the library.
        cursor.execute(
            "SELECT name, setting::bigint FROM pg_settings "
            "WHERE name IN ('statement_timeout', 'lock_timeout')"
        )
        values = dict(cursor.fetchall())
        return values['statement_timeout'], values['lock_timeout']

    def assert_timeouts(self, statement, lock, *, using='default'):
        with connections[using].cursor() as cursor:
            self.assertEqual(self.read_timeouts(cursor), (statement, lock))

    def test_unlimited_stricter_equal_and_looser_settings(self):
        cases = (
            ('0', '0', (1000, 100)),
            ('600ms', '40ms', (600, 40)),
            ('1000ms', '100ms', (1000, 100)),
            ('2s', '200ms', (1000, 100)),
            ('600ms', '200ms', (600, 100)),
            ('2s', '40ms', (1000, 40)),
        )
        for statement, lock, expected in cases:
            with self.subTest(statement=statement, lock=lock), transaction.atomic():
                with connection.cursor() as cursor:
                    self.set_settings(cursor, statement, lock)
                    set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
                    self.assertEqual(self.read_timeouts(cursor), expected)
            self.assert_timeouts(0, 0)

    def test_postgresql_units_and_maximum_timeout(self):
        cases = (
            ('750ms', '75ms', (750, 75)),
            ('2s', '1s', (1000, 100)),
            ('3min', '2min', (1000, 100)),
            ('4h', '3h', (1000, 100)),
            ('2d', '1d', (1000, 100)),
            ('2147483647ms', '2147483647ms', (1000, 100)),
        )
        for statement, lock, expected in cases:
            with self.subTest(statement=statement, lock=lock), transaction.atomic():
                with connection.cursor() as cursor:
                    self.set_settings(cursor, statement, lock)
                    set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
                    self.assertEqual(self.read_timeouts(cursor), expected)

    def test_each_call_reads_the_current_settings(self):
        with transaction.atomic(), connection.cursor() as cursor:
            self.set_settings(cursor, '600ms', '40ms')
            set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
            self.assertEqual(self.read_timeouts(cursor), (600, 40))

            self.set_settings(cursor, '0', '0')
            set_local_timeouts(cursor, statement_timeout=5000, lock_timeout=2000)
            self.assertEqual(self.read_timeouts(cursor), (5000, 2000))

            self.set_settings(cursor, '2s', '1s')
            set_local_timeouts(cursor, statement_timeout=5000, lock_timeout=2000)
            self.assertEqual(self.read_timeouts(cursor), (2000, 1000))

    def test_commit_restores_session_settings(self):
        self.restore_settings('9s', '4s')
        with transaction.atomic(), connection.cursor() as cursor:
            set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
            self.assertEqual(self.read_timeouts(cursor), (1000, 100))
        self.assert_timeouts(9000, 4000)

    def test_rollback_restores_session_settings(self):
        self.restore_settings('9s', '4s')
        with self.assertRaisesRegex(RuntimeError, 'Discard the transaction'):
            with transaction.atomic(), connection.cursor() as cursor:
                set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
                self.assertEqual(self.read_timeouts(cursor), (1000, 100))
                raise RuntimeError('Discard the transaction.')
        self.assert_timeouts(9000, 4000)

    def test_savepoint_rollback_restores_and_commit_preserves_local_settings(self):
        self.restore_settings('9s', '4s')
        with transaction.atomic():
            with connection.cursor() as cursor:
                self.set_settings(cursor, '8s', '3s')
            with self.assertRaisesRegex(RuntimeError, 'Discard the savepoint'):
                with transaction.atomic(), connection.cursor() as cursor:
                    set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
                    self.assertEqual(self.read_timeouts(cursor), (1000, 100))
                    raise RuntimeError('Discard the savepoint.')
            self.assert_timeouts(8000, 3000)
            with transaction.atomic(), connection.cursor() as cursor:
                set_local_timeouts(cursor, statement_timeout=1000, lock_timeout=100)
            self.assert_timeouts(1000, 100)
        self.assert_timeouts(9000, 4000)

    def test_supervisor_applies_limits_and_restores_the_caller_settings(self):
        cases = (
            ('0', '0', (1000, 100)),
            ('600ms', '40ms', (600, 40)),
            ('3s', '500ms', (1000, 100)),
        )
        for statement, lock, expected in cases:
            with self.subTest(statement=statement, lock=lock):
                self.restore_settings(statement, lock)
                with connection.cursor() as cursor:
                    original = self.read_timeouts(cursor)
                with pull._supervisor_transaction('default'):
                    self.assert_timeouts(*expected)
                self.assert_timeouts(*original)

    def test_fair_claim_limits_apply_during_selection_only(self):
        original_claim = fairness._claim_in_transaction
        cases = (
            ('0', '0', (1000, 100)),
            ('600ms', '40ms', (600, 40)),
            ('3s', '500ms', (1000, 100)),
        )
        for statement, lock, expected in cases:
            with self.subTest(statement=statement, lock=lock):
                self.restore_settings(statement, lock)
                with connection.cursor() as cursor:
                    original = self.read_timeouts(cursor)
                observed = []

                def inspect_selection(rows, groups, started):
                    self.assertTrue(connection.in_atomic_block)
                    with connection.cursor() as cursor:
                        observed.append(self.read_timeouts(cursor))
                    return original_claim(rows, groups, started)

                with patch.object(fairness, '_claim_in_transaction', side_effect=inspect_selection):
                    self.assertIsNone(fairness.claim_fair(TransitionMessage.objects.filter(pk=-1)))
                self.assertEqual(observed, [expected])
                self.assert_timeouts(*original)

    def test_independent_lock_connection_preserves_its_stricter_defaults(self):
        cases = (
            ('0', '0', (5000, 2000)),
            ('600ms', '40ms', (600, 40)),
            ('5s', '2s', (5000, 2000)),
            ('9s', '4s', (5000, 2000)),
        )
        options = connection.settings_dict['OPTIONS']
        existing = options.get('options', '')
        for statement, lock, expected in cases:
            with self.subTest(statement=statement, lock=lock):
                configured = f'{existing} -c statement_timeout={statement} -c lock_timeout={lock}'
                with patch.dict(options, {'options': configured}):
                    with _lock_connection() as alias:
                        self.assertNotEqual(alias, 'default')
                        self.assertTrue(connections[alias].in_atomic_block)
                        self.assert_timeouts(*expected, using=alias)
                self.assert_timeouts(0, 0)
