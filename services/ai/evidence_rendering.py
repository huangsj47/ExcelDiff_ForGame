"""Evidence facts are lossless; only downstream addressable delivery may paginate."""
from contextvars import ContextVar
from functools import wraps

FULL_EVIDENCE = ContextVar("full_diff_evidence", default=False)


def lossless_evidence(function):
    @wraps(function)
    def render(*args, **kwargs):
        token = FULL_EVIDENCE.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            FULL_EVIDENCE.reset(token)
    return render
