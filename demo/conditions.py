def is_staff(lock, user):
    return user.is_staff


def is_user(lock, user):
    return not user.is_staff


def is_planned(lock):
    return lock.customer_received_notice


def is_lock_available(lock):
    return lock.is_available
