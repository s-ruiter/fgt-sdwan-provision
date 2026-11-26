"""Cancelling a running deployment: the job's threads wait through cancel.sleep, which wakes up at once on a cancel.

The cancel event of the job is bound to the job's thread (a context variable); worker threads the pipeline starts
(ThreadPoolExecutor) get it through wrap(). Without a bound job sleep is a plain time.sleep. Cancelled is a
BaseException, so the pipeline's `except Exception` retries never swallow it.
"""
import contextvars
import time

_EV = contextvars.ContextVar("fgt_sdwan_cancel", default=None)


class Cancelled(BaseException):
    pass


def bind(event):
    """Bind the cancel event (threading.Event or None) to the current thread / context."""
    _EV.set(event)


def check():
    ev = _EV.get()
    if ev is not None and ev.is_set():
        raise Cancelled("cancelled by the user")


def sleep(seconds):
    ev = _EV.get()
    if ev is None:
        time.sleep(seconds)
        return
    if ev.wait(max(0, seconds)):
        raise Cancelled("cancelled by the user")


def wrap(fn):
    """fn for a worker thread, running in a copy of the caller's context (so it sees the job's cancel event)."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.run(fn, *a, **k)
