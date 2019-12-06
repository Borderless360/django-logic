from django.core.cache import cache


class State(object):
    @staticmethod
    def get_db_state(instance, field_name):
        """
        Fetches state directly from db instead of model instance.
        """
        return instance._meta.model.objects.values_list(field_name, flat=True).get(pk=instance.id)

    @staticmethod
    def set_state(instance, field_name, state):
        """
        Sets intermediate state to instance's field until transition is over.
        """
        # TODO: how would it work if it's used within another transaction?
        instance._meta.model.objects.filter(pk=instance.id).update(**{field_name: state})
        instance.refresh_from_db()

    def get_hash(self, instance, field_name):
        # TODO: https://github.com/Borderless360/django-logic/issues/3
        return "{}-{}-{}-{}".format(instance._meta.app_label,
                                    instance._meta.model_name,
                                    field_name,
                                    instance.pk)

    def lock(self, instance, field_name: str):
        cache.set(self.get_hash(instance, field_name), True)

    def unlock(self, instance, field_name: str):
        cache.delete(self.get_hash(instance, field_name))

    def is_locked(self, instance, field_name: str):
        return cache.get(self.get_hash(instance, field_name)) or False