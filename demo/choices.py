from model_utils import Choices


LOCK_STATES = Choices(
    ('maintenance', 'Under maintenance'),
    ('locked', 'Locked'),
    ('open', 'Open'),
)
