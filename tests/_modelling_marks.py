"""Marks for tests that need the private aihydro-modelling package.

``needs_modelling(min_version_check=None)`` skips when the package is absent (or ``check()`` is
false) AND carries the ``needs_modelling`` marker, which tests/conftest.py turns into a failure
when AIHYDRO_REQUIRE_MODELLING=1. Match on the marker, never on the skip text.
"""
from __future__ import annotations

import importlib.util
from typing import Callable

import pytest


def modelling_marks(check: Callable[[], bool] | None = None,
                    reason: str = "needs the optional aihydro-modelling package (private; not installable in public CI)"):
    """The marks as a list (for ``pytest.param(..., marks=...)``)."""
    present = importlib.util.find_spec("aihydro_modelling") is not None
    ok = present and (check() if check else True)
    return [pytest.mark.skipif(not ok, reason=reason), pytest.mark.needs_modelling]


def modelling_mark(check: Callable[[], bool] | None = None, reason: str | None = None):
    """The same marks as a decorator."""
    marks = modelling_marks(check, *( [reason] if reason else [] ))

    def deco(obj):
        for m in marks:
            obj = m(obj)
        return obj

    return deco
