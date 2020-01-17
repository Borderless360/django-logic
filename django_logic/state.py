import logging
from hashlib import blake2b
from django.core.cache import cache


class State(object):
    def __init__(self, instance: any, field_name: str, queryset=None):
        self.instance = instance
        self.queryset = queryset or instance._meta.model.objects.all()
        self.field_name = field_name

    def get_db_state(self):
        """
        Fetches state directly from db instead of model instance.
        """
        return self.queryset.values_list(self.field_name, flat=True).get(pk=self.instance.id)

    @property  # TODO: change to cached
    def cached_state(self):
        return self.get_db_state()

    def set_state(self, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        # TODO: how would it work if it's used within another transaction?
        self.queryset.filter(pk=self.instance.id).update(**{self.field_name: state})
        self.instance.refresh_from_db()

    def _get_hash(self):
        key = "{}-{}-{}-{}".format(self.instance._meta.app_label,
                                   self.instance._meta.model_name,
                                   self.field_name,
                                   self.instance.pk)
        return blake2b(key.encode(), digest_size=16).hexdigest()

    def lock(self):
        cache.set(self._get_hash(), True)

    def unlock(self):
        cache.delete(self._get_hash())

    def is_locked(self):
        return cache.get(self._get_hash()) or False
