"""Give an application a bounded chance to send a job process's reports."""
import os
import struct
import threading
import time

from django_logic.logger import logger

FINISH_SECONDS = 2.0
WORK_RESULT = struct.Struct('!Bd')


def finish_job_process(finish, result_fd, status):
    """Report the job outcome before running the application's finish function."""
    started = time.monotonic()
    try:
        os.write(result_fd, WORK_RESULT.pack(status, started))
    except OSError:
        # The worker may have stopped. Sending reports still remains useful.
        pass
    finally:
        os.close(result_fd)

    def send_reports():
        try:
            finish()
        except BaseException:
            # A reporting failure must not replace the recorded job outcome.
            logger.exception('pull: JOB_PROCESS_FINISH failed to send job reports.')

    sender = threading.Thread(target=send_reports, daemon=True)
    sender.start()
    sender.join(max(0.0, FINISH_SECONDS - (time.monotonic() - started)))
