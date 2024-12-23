import logging
from abc import ABC, abstractmethod

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_logic.constants import LogType

DISABLE_LOGGING = getattr(settings, 'DJANGO_LOGIC_DISABLE_LOGGING', False)
CUSTOM_LOGGER = getattr(settings, 'DJANGO_LOGIC_CUSTOM_LOGGER', None)


class AbstractLogger(ABC):
    def __init__(self, **kwargs):
        pass

    @abstractmethod
    def info(self, message: str, **kwargs) -> None:
        pass

    @abstractmethod
    def error(self, exception: BaseException, **kwargs) -> None:
        pass


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


class NullLogger(AbstractLogger):
    """ Logger that doesn't write messages """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def info(self, message: str, **kwargs) -> None:
        pass

    def error(self, exception: BaseException, **kwargs) -> None:
        pass


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
