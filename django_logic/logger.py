import logging
from abc import ABC, abstractmethod

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

DISABLE_LOGGING = getattr(settings, 'DJANGO_LOGIC_DISABLE_LOGGING', False)
CUSTOM_LOGGER = getattr(settings, 'DJANGO_LOGIC_CUSTOM_LOGGER', None)


class AbstractLogger(ABC):
    @abstractmethod
    def log(self, message: str) -> None:
        pass

    @abstractmethod
    def error(self, exception: BaseException) -> None:
        pass


class DefaultLogger(AbstractLogger):
    """ Logger that uses root logging settings """
    logger = None

    def __init__(self, **kwargs):
        module_name = kwargs.get('module_name', '')
        self.logger = logging.getLogger(module_name)

    def log(self, message: str) -> None:
        self.logger.info(message)

    def error(self, exception: BaseException) -> None:
        self.logger.exception(exception)


class NullLogger(AbstractLogger):
    """ Logger that doesn't write messages """

    def log(self, message: str) -> None:
        pass

    def error(self, exception: BaseException) -> None:
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
