"""Apply PostgreSQL limits without scanning the server settings catalog."""


def set_local_timeouts(cursor, *, statement_timeout, lock_timeout):
    """Limit this transaction's waits without relaxing a stricter current limit."""
    # PostgreSQL reports units such as ms, s and min. Zero means unlimited.
    cursor.execute(
        "SELECT set_config('statement_timeout', "
        "LEAST(NULLIF(EXTRACT(EPOCH FROM current_setting('statement_timeout')::interval) * 1000, 0), "
        "%s)::bigint::text, true), "
        "set_config('lock_timeout', "
        "LEAST(NULLIF(EXTRACT(EPOCH FROM current_setting('lock_timeout')::interval) * 1000, 0), "
        "%s)::bigint::text, true)",
        [statement_timeout, lock_timeout],
    )
