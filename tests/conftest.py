"""Suite-wide test isolation and approval fixtures (Slice 1b, ADR-002a).

Every test runs with its own ``AIHYDRO_HOME``, so the claim registry and the
human-approval store resolve to a throwaway directory and never to the user's
real ``~/.aihydro``. The registry and approval paths are resolved at call
time, so setting the variable per test is enough.

A suite-level default is also set at import, so anything touched during
collection or by a module-level import is isolated too.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_SUITE_HOME = tempfile.mkdtemp(prefix="aihydro-test-home-")
atexit.register(shutil.rmtree, _SUITE_HOME, ignore_errors=True)
os.environ["AIHYDRO_HOME"] = _SUITE_HOME


@pytest.fixture(autouse=True)
def _isolated_aihydro_home(tmp_path_factory, monkeypatch):
    """Fresh ``AIHYDRO_HOME`` per test (registry + approvals)."""
    home = tmp_path_factory.mktemp("aihydro_home")
    monkeypatch.setenv("AIHYDRO_HOME", str(home))
    # Approvals fail closed by default (ADR-002b). Legacy v1 approval fixtures
    # run under the explicit, stamped development opt-out; signing tests
    # override both variables themselves. A real /etc/aihydro trust root on
    # the developer's machine must never leak into the suite.
    monkeypatch.setenv("AIHYDRO_REQUIRE_SIGNED", "0")
    monkeypatch.setenv("AIHYDRO_SYSTEM_TRUST_FILE", str(home / "no-system-trust"))
    return home


@pytest.fixture
def approve_claim():
    """Fixture form of :func:`approval_helpers.approve` (see that module)."""
    from approval_helpers import approve

    return approve
