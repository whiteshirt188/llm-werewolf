"""App factory for existing phase-focused regressions; opening has dedicated integration coverage."""

from werewolf.api import create_app


def flow_app(*args, **kwargs):
    kwargs.setdefault("opening_enabled", False)
    return create_app(*args, **kwargs)
