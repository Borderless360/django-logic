from hashlib import blake2b
from django.core.cache import cache
from django.utils.functional import cached_property


class State(object):
    def __init__(self, instance: any, field_name: str, process_name=None, queryset_name=None):
        self.instance = instance
        self.queryset_name = queryset_name or 'objects'
        self.field_name = field_name
        self.process_name = process_name

    def get_queryset(self):
        return getattr(self.instance._meta.model, self.queryset_name).all()

    def get_db_state(self):
        """
        Fetches state directly from db instead of model instance.
        """
        return self.get_queryset().values_list(self.field_name, flat=True).get(pk=self.instance.id)

    @cached_property
    def cached_state(self):
        return self.get_db_state()

    def set_state(self, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        # TODO: how would it work if it's used within another transaction?
        self.get_queryset().filter(pk=self.instance.id).update(**{self.field_name: state})
        self.instance.refresh_from_db()

    @property
    def instance_key(self):
        return f'{self.instance._meta.app_label}-' \
               f'{self.instance._meta.model_name}-' \
               f'{self.field_name}-' \
               f'{self.instance.pk}'

    def get_log_data(self):
        return {
            'instance': self.instance,
            'queryset_name': self.queryset_name,
            'process_name': self.process_name,
            'field_name': self.field_name,
        }

    def _get_hash(self):
        return blake2b(self.instance_key.encode(), digest_size=16).hexdigest()

    def lock(self):
        """
        It locks the state for 3 years.
        It returns True if it's been locked and False otherwise.
        """
        cache.set(self._get_hash(), True, 99999999)
        return True

    def unlock(self):
        """
        It unclocks the current state
        """
        cache.delete(self._get_hash())

    def is_locked(self):
        """
        It checks whether the state was locked or not.
        It might return False due to the race conditions.
        However, `lock` method should guarantees it will be locked only once.
        """
        return cache.get(self._get_hash()) or False


class RedisState(State):
    """
    RedisState implements the optimistic locking of the state
    and guarantees to be locked only once.
    Basically, it provides a solution to the race conditions problem for the state
    being available in parallel execution of a transition.
    """
    def lock(self):
        """
        It locks the state only once for 3 years.
        nx - sets the value only once, if it was set up before it guarantees to return False.
        It returns True if it's been locked and False otherwise.
        """
        return cache.set(self._get_hash(), True, 99999999, nx=True) or False
