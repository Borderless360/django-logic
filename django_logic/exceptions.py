class DjangoLogicException(Exception):
    pass


class TransitionNotAllowed(DjangoLogicException):
    pass
