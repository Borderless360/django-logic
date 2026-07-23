"""django_logic.E002 (#148): database routers must not split the atomic
outbox across databases.

Phase 1 commits the instance's state write and the TransitionMessage row
in ONE transaction through unqualified managers and bare
``transaction.atomic()`` — both resolve to the 'default' alias. A router
that sends TransitionMessage (or a background-bound model) elsewhere
breaks that invariant silently, so the system check refuses the topology
outright. The check function is exercised directly: making split
topologies unsupported-by-refusal is the whole fix, no two-database
runtime integration is needed.
"""
from django.test import SimpleTestCase, override_settings

from django_logic.checks import check_background_database_routing


class _TransitionMessageElsewhereRouter:
    """Sends the background outbox to a different alias."""

    def db_for_read(self, model, **hints):
        if model._meta.app_label == 'django_logic_background':
            return 'other'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == 'django_logic_background':
            return 'other'
        return None


class _TransitionMessageReadElsewhereRouter:
    """Splits reads from writes for the outbox (read replica pattern)."""

    def db_for_read(self, model, **hints):
        if model._meta.app_label == 'django_logic_background':
            return 'other'
        return None

    def db_for_write(self, model, **hints):
        return None


class _BackgroundModelElsewhereRouter:
    """Keeps TransitionMessage on 'default' but sends a background-bound
    model's writes elsewhere."""

    def db_for_read(self, model, **hints):
        return None

    def db_for_write(self, model, **hints):
        if model._meta.label == 'bg_tests.Widget':
            return 'other'
        return None


class RouterCheckTests(SimpleTestCase):
    def _findings(self):
        return [f for f in check_background_database_routing(None)
                if f.id == 'django_logic.E002']

    def test_default_no_router_setup_has_no_findings(self):
        self.assertEqual(self._findings(), [])

    @override_settings(
        DATABASE_ROUTERS=[_TransitionMessageElsewhereRouter()])
    def test_transition_message_routed_elsewhere_is_refused(self):
        findings = self._findings()
        self.assertTrue(findings)
        tm_findings = [f for f in findings
                       if 'TransitionMessage' in f.obj]
        self.assertEqual(len(tm_findings), 1)
        self.assertIn('atomic outbox', tm_findings[0].msg)
        self.assertIn("'other'", tm_findings[0].msg)

    @override_settings(
        DATABASE_ROUTERS=[_TransitionMessageReadElsewhereRouter()])
    def test_split_read_write_for_transition_message_is_refused(self):
        # Even a read replica split is unsupported: the runtime re-reads
        # rows it just wrote inside the same logical operation.
        findings = [f for f in self._findings()
                    if 'TransitionMessage' in f.obj]
        self.assertEqual(len(findings), 1)

    @override_settings(
        DATABASE_ROUTERS=[_BackgroundModelElsewhereRouter()])
    def test_background_bound_model_routed_elsewhere_is_refused(self):
        findings = self._findings()
        widget_findings = [f for f in findings
                           if f.obj == 'bg_tests.Widget']
        self.assertEqual(len(widget_findings), 1)
        self.assertIn('atomic outbox', widget_findings[0].msg)
        # TransitionMessage itself stayed on 'default' — no finding for it.
        self.assertEqual(
            [f for f in findings if 'TransitionMessage' in f.obj], [])
        # Models NOT bound to a background-transition process (e.g. the
        # sync-only Invoice fixtures) must not be flagged.
        self.assertEqual(
            [f for f in findings if f.obj.startswith('tests.')], [])
