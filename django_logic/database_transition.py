"""Synchronous PostgreSQL changes whose work can roll back with the transaction."""
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections

from django_logic.state import _database_only_scope
from django_logic.transition import Transition


class DatabaseTransition(Transition):
    """Change local records using PostgreSQL row locks without creating cache locks.

    All nested transitions and submissions share this database-only contract.
    External writes need separate background work outside this transaction.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.lock:
            raise ImproperlyConfigured('DatabaseTransition requires its database lock; remove lock=False.')

    def change_state(self, state, **kwargs):
        using = state.instance._state.db or DEFAULT_DB_ALIAS
        if connections[using].vendor != 'postgresql':
            raise ImproperlyConfigured('DatabaseTransition requires PostgreSQL row locks.')
        return super().change_state(state, **kwargs)

    def _change_state(self, state, **kwargs):
        # Commit callbacks must run outside the database-only promise.
        with _database_only_scope(state):
            return super()._change_state(state, **kwargs)
