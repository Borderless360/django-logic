"""Submission preparation uses the same transaction as the saved job."""
from tests import unbind_model_process
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import threading
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from django_logic import Process, ProcessManager
from django_logic.background import BackgroundTransition, Defer
from django_logic.background.models import TransitionMessage
from django_logic.background.exceptions import AlreadyInProgress
from django_logic.background.runner import run_background_transition
from django_logic.exceptions import TransitionNotAllowed
from django.utils import timezone
from tests import dl_settings
from tests.background.models import Widget
from tests.stability.base import requires_postgres


def change_nested_argument(instance, **kwargs):
    kwargs['snapshot']['members'].append('changed during execution')


def save_execution_argument(instance, **kwargs):
    instance.kwargs_seen = kwargs['snapshot']['members']
    instance.save(update_fields=['kwargs_seen'])


class SubmissionProcess(Process):
    process_name = 'submission'
    transitions = [BackgroundTransition('finish', sources=['draft'], target='done')]


class SubmissionTestSetup:
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        ProcessManager.bind_model_process(Widget, SubmissionProcess, state_field='status')
        self.addCleanup(unbind_model_process, Widget, SubmissionProcess)
        self.widget = Widget.objects.create()

    def declaration(self, **kwargs):
        transition = BackgroundTransition('finish', sources=['draft'], target='done',
                                          in_progress_state='working', **kwargs)
        self.enterContext(patch.object(SubmissionProcess, 'transitions', [transition]))
        return transition


@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class SubmissionContextTests(SubmissionTestSetup, TransactionTestCase):
    def test_context_prepares_saved_inputs_and_records_acceptance_after_commit(self):
        events = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            self.assertTrue(transaction.get_connection(using).in_atomic_block)
            self.assertIs(transition, declaration)
            self.assertIs(state.instance, self.widget)
            kwargs['snapshot'] = {'members': ['original']}
            yield
            self.assertEqual(Widget.objects.get(pk=self.widget.pk).status, 'working')
            self.assertEqual(TransitionMessage.objects.get().kwargs['snapshot'], kwargs['snapshot'])
            transaction.on_commit(lambda: events.append('accepted'), using=using)

        declaration = self.declaration(submission_context=prepare)
        with transaction.atomic():
            self.widget.submission.finish(snapshot={'members': ['caller override']})
            self.assertEqual(events, [])
        self.assertEqual(events, ['accepted'])
        self.assertEqual(TransitionMessage.objects.get().kwargs['snapshot'], {'members': ['original']})

    def test_execution_mutation_does_not_change_saved_inputs(self):
        self.declaration(side_effects=[change_nested_argument, save_execution_argument])
        self.widget.submission.finish(snapshot={'members': ['original']})
        job = TransitionMessage.objects.get()
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.kwargs['snapshot'], {'members': ['original']})
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.kwargs_seen, ['original', 'changed during execution'])

    def test_context_failure_before_or_after_yield_rolls_back_everything(self):
        for when in ('before', 'after'):
            with self.subTest(when=when):
                events = []

                @contextmanager
                def prepare(transition, state, kwargs, *, using):
                    Widget.objects.using(using).filter(pk=state.instance.pk).update(se_log='prepared')
                    transaction.on_commit(lambda: events.append('accepted'), using=using)
                    if when == 'before':
                        raise TransitionNotAllowed('not permitted')
                    yield
                    raise RuntimeError('history preparation failed')

                self.declaration(submission_context=prepare)
                with self.assertRaises((TransitionNotAllowed, RuntimeError)):
                    self.widget.submission.finish()
                self.widget.refresh_from_db()
                self.assertEqual((self.widget.status, self.widget.se_log), ('draft', ''))
                self.assertEqual(events, [])
                self.assertFalse(TransitionMessage.objects.exists())
                self.assertFalse(self.widget.submission.state.is_locked())

    def test_outer_rollback_discards_saved_job_and_history_callback(self):
        events = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            yield
            transaction.on_commit(lambda: events.append('accepted'), using=using)

        self.declaration(submission_context=prepare)
        with self.assertRaisesMessage(RuntimeError, 'caller rolled back'):
            with transaction.atomic():
                self.widget.submission.finish()
                raise RuntimeError('caller rolled back')
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())
        self.assertEqual(events, [])

    def test_existing_unfinished_job_does_not_prepare_again(self):
        events = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            events.append('entered')
            yield
            transaction.on_commit(lambda: events.append('accepted'), using=using)

        declaration = self.declaration(submission_context=prepare)
        declaration.in_progress_state = None
        self.widget.submission.finish()
        for mode in ('pull', 'sync'):
            with self.subTest(mode=mode), self.settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION=mode)):
                with self.assertRaises(AlreadyInProgress):
                    self.widget.submission.finish()
        self.assertEqual(events, ['entered', 'accepted'])
        self.assertEqual(TransitionMessage.objects.count(), 1)

    def test_preparation_integrity_error_is_not_reported_as_an_existing_job(self):
        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            Widget.objects.using(using).create(status=None)
            yield

        self.declaration(submission_context=prepare)
        with self.assertRaises(IntegrityError):
            self.widget.submission.finish()
        self.assertFalse(TransitionMessage.objects.exists())

    def test_invalid_generated_argument_is_refused_before_state_or_job_is_saved(self):
        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            kwargs['invalid'] = object()
            yield

        self.declaration(submission_context=prepare)
        with self.assertRaisesMessage(ImproperlyConfigured, 'not JSON-serializable'):
            self.widget.submission.finish()
        self.widget.refresh_from_db()
        self.assertEqual(self.widget.status, 'draft')
        self.assertFalse(TransitionMessage.objects.exists())

    def test_context_cannot_change_engine_identity_before_or_after_yield(self):
        for key in ('process_class', 'owning_process_class', 'tr_id', 'root_id', 'parent_id', 'user_id', 'user'):
            for after in (False, True):
                with self.subTest(key=key, after=after):
                    @contextmanager
                    def prepare(transition, state, kwargs, *, using):
                        if not after:
                            kwargs[key] = 'forged'
                        yield
                        if after:
                            kwargs[key] = 'forged'

                    self.declaration(submission_context=prepare)
                    with self.assertRaisesMessage(ImproperlyConfigured, 'job identity or user'):
                        self.widget.submission.finish()
                    self.widget.refresh_from_db()
                    self.assertEqual(self.widget.status, 'draft')
                    self.assertFalse(TransitionMessage.objects.exists())

    def test_context_cannot_change_bound_instance(self):
        other = Widget.objects.create()

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            state.instance = other
            yield

        self.declaration(submission_context=prepare)
        with self.assertRaisesMessage(ImproperlyConfigured, 'job identity or user'):
            self.widget.submission.finish()
        self.assertFalse(TransitionMessage.objects.exists())

    def test_context_cannot_suppress_a_serialization_error(self):
        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            kwargs['invalid'] = object()
            try:
                yield
            except ImproperlyConfigured:
                pass

        self.declaration(submission_context=prepare)
        with self.assertRaisesMessage(ImproperlyConfigured, 'must not suppress'):
            self.widget.submission.finish()
        self.assertFalse(TransitionMessage.objects.exists())

    def test_context_cannot_silently_roll_back(self):
        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            yield
            transaction.set_rollback(True, using=using)

        self.declaration(submission_context=prepare)
        with self.assertRaisesMessage(ImproperlyConfigured, 'silently roll back'):
            self.widget.submission.finish()
        self.assertFalse(TransitionMessage.objects.exists())

    def test_context_keeps_typed_arguments_and_does_not_mutate_caller_values(self):
        value = {'members': ['caller']}

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            kwargs['snapshot']['members'].append(Decimal('1.25'))
            yield

        self.declaration(submission_context=prepare)
        self.widget.submission.finish(snapshot=value)
        from django_logic.background.serializers import deserialize_kwargs
        self.assertEqual(deserialize_kwargs(TransitionMessage.objects.get().kwargs)['snapshot'],
                         {'members': ['caller', Decimal('1.25')]})
        self.assertEqual(value, {'members': ['caller']})

    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync'))
    def test_sync_work_runs_after_submission_context_and_transaction_end(self):
        active = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            active.append(True)
            yield
            active.pop()

        def provider(instance, **kwargs):
            self.assertEqual(active, [])
            # Atomic work has its own transaction; it must not be the submission one.
            self.assertTrue(TransitionMessage.objects.get().pk)

        self.declaration(submission_context=prepare, side_effects=[provider])
        self.widget.submission.finish()
        self.assertTrue(TransitionMessage.objects.get().is_completed)

    def test_retry_and_existing_old_format_job_do_not_prepare_again(self):
        events = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            events.append('entered')
            kwargs['snapshot'] = {'members': ['new']}
            yield

        declaration = self.declaration()
        self.widget.submission.finish(snapshot={'members': ['old']})
        job = TransitionMessage.objects.get()
        declaration.submission_context = prepare
        with patch.object(declaration.side_effects, '_commands', [lambda instance, **kwargs: (_ for _ in ()).throw(
                Defer(until=timezone.now() + timedelta(seconds=1), reason='wait'))]):
            run_background_transition(job.pk)
        TransitionMessage.objects.filter(pk=job.pk).update(eligible_at=timezone.now() - timedelta(seconds=1))
        run_background_transition(job.pk)
        job.refresh_from_db()
        self.assertTrue(job.is_completed)
        self.assertEqual(job.kwargs['snapshot'], {'members': ['old']})
        self.assertEqual(events, [])

    def test_invalid_context_declaration_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'must be callable'):
            self.declaration(submission_context=42)


@requires_postgres
@override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='pull'))
class SubmissionContextPostgresTests(SubmissionTestSetup, TransactionTestCase):
    @override_settings(DJANGO_LOGIC=dl_settings(BACKGROUND_EXECUTION='sync'))
    def test_non_atomic_provider_runs_with_autocommit_after_context_releases_row(self):
        active = []

        @contextmanager
        def prepare(transition, state, kwargs, *, using):
            Widget.objects.using(using).select_for_update().get(pk=state.instance.pk)
            active.append(True)
            yield
            active.pop()

        def provider(instance, **kwargs):
            self.assertFalse(active)
            self.assertTrue(connection.get_autocommit())
            observer = connections['default'].copy(alias='test_submission_observer')
            try:
                with observer.cursor() as cursor:
                    cursor.execute(f'SELECT id FROM {Widget._meta.db_table} WHERE id = %s FOR UPDATE NOWAIT',
                                   [instance.pk])
                    self.assertEqual(cursor.fetchone(), (instance.pk,))
            finally:
                observer.close()

        self.declaration(submission_context=prepare, side_effects=[provider], atomic=False)
        self.widget.submission.finish()
        self.assertTrue(TransitionMessage.objects.get().is_completed)

    def test_public_concurrent_submissions_prepare_and_commit_once(self):
        for target in ('done', None):
            for caller_transaction in (False, True):
                with self.subTest(target=target, caller_transaction=caller_transaction):
                    self.widget = Widget.objects.create()
                    barrier = threading.Barrier(2)
                    prepared = threading.Event()
                    allow_commit = threading.Event()
                    accepted, failures, results, preparations = [], [], [], []

                    def ready(instance, **kwargs):
                        barrier.wait(timeout=10)
                        return True

                    @contextmanager
                    def prepare(transition, state, kwargs, *, using):
                        preparations.append(state.instance.pk)
                        Widget.objects.using(using).filter(pk=state.instance.pk).update(se_log='prepared')
                        transaction.on_commit(lambda: accepted.append('accepted'), using=using)
                        prepared.set()
                        yield
                        if not caller_transaction:
                            self.assertTrue(allow_commit.wait(10))

                    declaration = self.declaration(submission_context=prepare, conditions=[ready])
                    declaration.target = target
                    declaration.in_progress_state = None

                    def submit():
                        try:
                            widget = Widget.objects.get(pk=self.widget.pk)
                            if caller_transaction:
                                with transaction.atomic():
                                    result = widget.submission.finish()
                                    self.assertTrue(allow_commit.wait(10))
                            else:
                                result = widget.submission.finish()
                            results.append(result)
                        except BaseException as error:
                            failures.append(error)
                        finally:
                            connections.close_all()

                    threads = [threading.Thread(target=submit) for _ in range(2)]
                    try:
                        for thread in threads:
                            thread.start()
                        self.assertTrue(prepared.wait(10))
                    finally:
                        allow_commit.set()
                        for thread in threads:
                            thread.join(15)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], AlreadyInProgress)
                    job = TransitionMessage.for_instance(self.widget, 'submission').get()
                    self.assertEqual([str(result) for result in results], [job.kwargs['tr_id']])
                    self.assertEqual(preparations, [self.widget.pk])
                    self.assertEqual(accepted, ['accepted'])
                    self.widget.refresh_from_db()
                    self.assertEqual((self.widget.status, self.widget.se_log), ('draft', 'prepared'))
