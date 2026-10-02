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


# ── Offline guard ───────────────────────────────────────────────────────────
# Tests not marked ``live`` must never reach the network: a stalled upstream
# turns an offline suite into a hang. Outbound connects/DNS lookups to
# non-loopback hosts fail fast, naming the offending test. Unix sockets and
# localhost / 127.0.0.1 / ::1 stay allowed (local servers, subprocess IPC).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class NetworkAccessBlocked(RuntimeError):
    """Raised when a non-``live`` test attempts outbound network access."""


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        return host.decode() if isinstance(host, bytes) else host
    return None  # str/bytes path (AF_UNIX) or unknown -> not network


def _is_local(host) -> bool:
    return host is None or str(host).lower() in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def _block_network_for_offline_tests(request, monkeypatch):
    """Fail fast if a test not marked ``live`` touches the network."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    import socket

    test_id = request.node.nodeid
    attempts: list[str] = []

    def _refuse(host, port=None):
        msg = (
            f"{test_id}: outbound network access to {host!r}"
            f"{'' if port is None else f' port {port}'} blocked. Tests not "
            "marked `live` must run offline: mock the network at the lowest "
            "layer, or mark the test `live`."
        )
        attempts.append(msg)
        raise NetworkAccessBlocked(msg)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_connect(self, address)

    def connect_ex(self, address):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, port, *args, **kwargs):
        if not _is_local(host):
            _refuse(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    real_create_connection = socket.create_connection
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    # Library code often wraps/swallows the connect error (retry chains,
    # fallbacks); an attempt that was caught is still an offline violation.
    if attempts:
        pytest.fail(f"{len(attempts)} blocked network attempt(s); first: {attempts[0]}", pytrace=False)
