from django.core.cache import cache

from django_logic.commands import SideEffects, Callbacks, Permissions, Conditions
from django_logic.exceptions import TransitionNotAllowed


class Transition(object):
    """
    Transition could be defined as a class or as an object and used as an object
    - action name
    - transitions name
    - target
    - it changes the the state of the object from source to target by triggering available action via transition name
    - validation if the action is available throughout permissions and conditions
    - run side effects and call backs
    """
    side_effects = SideEffects()
    callbacks = Callbacks()
    permissions = Permissions()
    conditions = Conditions()

    def __init__(self, action_name, sources, target, **kwargs):
        self.action_name = action_name
        self.target = target
        self.sources = sources
        self.in_progress_state = kwargs.get('in_progress_state')
        self.failed_state = kwargs.get('failed_state')
        self.failure_handler = kwargs.get('failure_handler')
        self.side_effects = kwargs.get('side_effects', [])
        self.callbacks = kwargs.get('callbacks', [])
        self.permissions = kwargs.get('permissions', [])
        self.conditions = kwargs.get('conditions', [])

    def __str__(self):
        return "Transition: {} to {}".format(self.action_name, self.target)

    def validate(self, instance: any, field_name: str, user=None) -> bool:
        """
        It validates this process to meet conditions and pass permissions
        :param field_name:
        :param instance: any instance used to meet conditions
        :param user: any object used to pass permissions
        :return: True or False
        """
        return (not self._is_locked(instance, field_name) and
                self.permissions.execute(instance, user) and
                self.conditions.execute(instance))

    def _get_hash(self, instance, field_name):
        # TODO: https://github.com/Borderless360/django-logic/issues/3
        return "{}-{}-{}-{}".format(instance._meta.app_label,
                                    instance._meta.model_name,
                                    field_name,
                                    instance.pk)

    def _lock(self, instance, field_name: str):
        cache.set(self._get_hash(instance, field_name), True)

    def _unlock(self, instance, field_name: str):
        cache.delete(self._get_hash(instance, field_name))

    def _is_locked(self, instance, field_name: str):
        return cache.get(self._get_hash(instance, field_name)) or False

    def _get_db_state(self, instance, field_name):
        """
        Fetches state directly from db instead of model instance.
        """
        return instance._meta.model.objects.values_list(field_name, flat=True).get(pk=instance.id)

    def _set_state(self, instance, field_name, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        # TODO: how would it work if it's used within another transaction?
        instance._meta.model.objects.filter(pk=instance.id).update(**{field_name: state})
        instance.refresh_from_db()
        
    def change_state(self, instance, field_name):
        """
        This method changes a state of the provided instance and file name by the following algorithm:
        - Lock state
        - Change state to `in progress` if such exists
        - Run side effects which should run `complete_transition` in case of success
        or `fail_transition` in case of failure.
        :param instance: any
        :param field_name: str
        """
        if self._is_locked(instance, field_name):
            raise TransitionNotAllowed("State is locked")

        self._lock(instance, field_name)
        if self.in_progress_state:
            self._set_state(instance, field_name, self.in_progress_state)
        self.side_effects.execute(instance, field_name)

    def complete_transition(self, instance, field_name):
        """
        It completes the transition process for provided instance and filed name.
        The instance will be unlocked and callbacks exc
        :param instance:
        :param field_name:
        :return:
        """
        self._set_state(instance, field_name, self.target)
        self._unlock(instance, field_name)
        self.callbacks.execute(instance, field_name)
    
    def fail_transition(self, instance, field_name):
        """
        It triggers fail transition in case of any failure during the side effects execution.
        :param instance: any
        :param field_name: str
        """
        if self.failed_state:
            self._set_state(instance, field_name, self.failed_state)
        self._unlock(instance, field_name)
