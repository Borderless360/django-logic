from enum import Enum


class LogType(Enum):
    TRANSITION_DEBUG = 'transition_debug',
    TRANSITION_ERROR = 'transition_error',
    TRANSITION_COMPLETED = 'transition_completed'
    TRANSITION_FAILED = 'transition_failed'

