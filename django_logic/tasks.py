from django.apps import apps
from django.db import transaction

from django_logic.commands import SideEffects, Callbacks


class TransitionTaskFailed(Exception):
    pass


try:
    from celery import signature, group, chain, shared_task
    from celery.result import AsyncResult
except ImportError:
    pass  # TODO: handle
else:
    @shared_task(acks_late=True)
    def complete_transition(*args, **kwargs):
        app = apps.get_app_config(kwargs['app_label'])
        model = app.get_model(kwargs['model_name'])
        instance = model.objects.get(id=kwargs['instance_id'])
        transition = kwargs['transition']

        transition.complete_transition(instance, kwargs['field_name'])


    @shared_task(acks_late=True)
    def fail_transition(task_id, *args, **kwargs):
        try:
            transition = kwargs['transition']
            try:
                # Exception passed through args
                exc = args[0]
            except IndexError:
                task = AsyncResult(task_id)
                exc = task.info

            app = apps.get_app_config(kwargs['app_label'])
            model = app.get_model(kwargs['model_name'])
            instance = model.objects.get(id=kwargs['instance_id'])
            transition.fail_transition(instance, kwargs['field_name'])
        except Exception:
            # TODO: add logger
            print('Exception')


class SideEffectTasks(SideEffects):
    def execute(self, instance: any, field_name: str, **kwargs):
        if not self.commands:
            return super(SideEffectTasks, self).execute(instance, field_name)

        task_kwargs = dict(app_label=instance._meta.app_label,
                           model_name=instance._meta.model_name,
                           instance_id=instance.pk,
                           field_name=field_name)

        header = [signature(task_name, kwargs=task_kwargs) for task_name in self.commands]
        header = chain(*header)
        task_kwargs.update(dict(transition=self.transition))
        body = complete_transition.s(**task_kwargs)
        tasks = chain(header | body).on_error(fail_transition.s(**task_kwargs))
        transaction.on_commit(tasks.delay)


class CallbacksTasks(Callbacks):
    def execute(self, instance, field_name: str, **kwargs):
        if not self.commands:
            return super(CallbacksTasks, self).execute(instance, field_name)

        task_kwargs = dict(app_label=instance._meta.app_label,
                           model_name=instance._meta.model_name,
                           instance_id=instance.pk,
                           field_name=field_name)

        tasks = [signature(task_name, kwargs=task_kwargs) for task_name in self.commands]
        transaction.on_commit(group(tasks))
