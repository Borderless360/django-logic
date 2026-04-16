from enum import Enum

# DEPRECATED
class LogType(Enum):
    TRANSITION_DEBUG = 'transition_debug'
    TRANSITION_ERROR = 'transition_error'
    TRANSITION_IN_PROGRESS = 'transition_in_progress'
    TRANSITION_COMPLETED = 'transition_completed'
    TRANSITION_FAILED = 'transition_failed'
