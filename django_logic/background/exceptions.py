from django_logic.exceptions import DjangoLogicException, TransitionNotAllowed


class BackgroundTransitionError(DjangoLogicException):
    """Base for background-transition-specific errors."""


class AlreadyInProgress(TransitionNotAllowed):
    """Raised by phase 1 when an uncompleted TransitionMessage already
    exists for the target instance (the partial unique constraint fires).
    """
