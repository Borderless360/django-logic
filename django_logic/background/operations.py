"""Validation for declared client fairness keys."""


def validate_key(value, name, *, allow_empty=False):
    if value is not None and (not isinstance(value, str) or len(value) > 255
                              or ('\x00' in value) or (not value and not allow_empty)):
        raise ValueError(f'{name} must be a string of 1 to 255 characters.')
    return value
