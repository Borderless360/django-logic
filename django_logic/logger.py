import logging
from abc import ABC, abstractmethod
from enum import Enum

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from django_logic.constants import LogType

# DEPRECATED
DISABLE_LOGGING = getattr(settings, 'DJANGO_LOGIC_DISABLE_LOGGING', False)
CUSTOM_LOGGER = getattr(settings, 'DJANGO_LOGIC_CUSTOM_LOGGER', None)

# DEPRECATED
class AbstractLogger(ABC):
    def __init__(self, **kwargs):
        pass

    @abstractmethod
    def info(self, message: str, **kwargs) -> None:
        pass

    @abstractmethod
    def error(self, exception: BaseException, **kwargs) -> None:
        pass

# DEPRECATED
class DefaultLogger(AbstractLogger):
    """ Logger that uses root logging settings """
    logger = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        module_name = kwargs.get('module_name', '')
        self.logger = logging.getLogger(module_name)

    def info(self, message: str, **kwargs) -> None:
        self.logger.info(message)

    def error(self, exception: BaseException, **kwargs) -> None:
        self.logger.exception(exception)

# DEPRECATED
class NullLogger(AbstractLogger):
    """ Logger that doesn't write messages """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def info(self, message: str, **kwargs) -> None:
        pass

    def error(self, exception: BaseException, **kwargs) -> None:
        pass

# DEPRECATED
def get_logger(**kwargs) -> AbstractLogger:
    if DISABLE_LOGGING:
        return NullLogger()

    if CUSTOM_LOGGER:
        try:
            custom_logger_class = import_string(CUSTOM_LOGGER)
        except ImportError as e:
            raise ImproperlyConfigured(f"Custom logger import error: {e}")
        return custom_logger_class(**kwargs)

    return DefaultLogger(**kwargs)

# The main logger for logging all activity of django-logic.
logger: logging.Logger = logging.getLogger('django-logic')
# A special logger for logging only activity of transitions.
transition_logger: logging.Logger = logging.getLogger('django-logic.transition')

class TransitionEventType(Enum):
    START = 'Start'
    COMPLETE = 'Complete'
    FAIL = 'Fail'
    SIDE_EFFECT = 'SideEffect'
    CALLBACK = 'Callback'
    FAILURE_SIDE_EFFECT = 'FailureSideEffect'
    SET_STATE = 'Set State'
    LOCK = 'Lock'
    UNLOCK = 'Unlock'
    NEXT_TRANSITION = 'Next Transition'
    BACKGROUND_MODE = 'Background Mode'