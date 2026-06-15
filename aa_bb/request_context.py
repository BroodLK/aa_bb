"""Request context storage for admin log attribution."""

# Standard Library
from contextvars import ContextVar

_current_user = ContextVar("aa_bb_current_user", default=None)
_current_request_meta = ContextVar("aa_bb_current_request_meta", default=None)


def set_request_context(user, meta):
    _current_user.set(user)
    _current_request_meta.set(meta)


def clear_request_context():
    _current_user.set(None)
    _current_request_meta.set(None)


def get_request_context():
    user = _current_user.get()
    meta = _current_request_meta.get() or {}
    return user, meta
