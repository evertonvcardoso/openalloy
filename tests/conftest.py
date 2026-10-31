# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
import faulthandler
import os
import tempfile
import threading
import time

import pytest

# A hung test must not stall the run silently: after this many seconds every thread's
# stack is printed and the run stops (override with OPENALLOY_TEST_TIMEOUT).
TEST_TIMEOUT = float(os.environ.get("OPENALLOY_TEST_TIMEOUT", "120"))
# pytest captures fd 2, so the stacks go to a file that survives the abrupt exit.
HANG_LOG = os.path.join(tempfile.gettempdir(), "openalloy-test-hang.log")
_hang_file = open(HANG_LOG, "w")  # noqa: SIM115 - kept open for faulthandler


def _openalloy_threads():
    return {t for t in threading.enumerate() if t.name.startswith("openalloy")}


@pytest.fixture(autouse=True)
def watchdog_and_leaks(request):
    """Per-test watchdog + no OpenAlloy thread may outlive its test."""
    before = _openalloy_threads()
    _hang_file.seek(0)
    _hang_file.truncate()
    _hang_file.write(f"hung test: {request.node.nodeid}\n")
    _hang_file.flush()
    faulthandler.dump_traceback_later(TEST_TIMEOUT, exit=True, file=_hang_file)
    yield
    faulthandler.cancel_dump_traceback_later()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        leaked = _openalloy_threads() - before
        if not leaked:
            return
        time.sleep(0.05)
    pytest.fail("threads still running after the test: "
                + ", ".join(sorted(t.name for t in leaked)), pytrace=False)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real ~/.config or runtime dir."""
    cfg = tmp_path / "config"
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.delenv("OPENALLOY_SOCKET", raising=False)
    monkeypatch.delenv("OPENALLOY_SIM_LAYOUT", raising=False)
    monkeypatch.delenv("OPENALLOY_LANG", raising=False)
    import logging

    from openalloy import i18n
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    i18n.set_language("en")
    yield tmp_path
    i18n.set_language("en")
    root.handlers[:] = handlers          # a CLI main() inside a test must not leak its handler
    root.setLevel(level)


@pytest.fixture
def sim():
    from openalloy.infra.simulator import SimulatedKeyboard
    return SimulatedKeyboard()


@pytest.fixture
def driver(sim):
    from openalloy.infra.drivers import open_driver
    d = open_driver(simulate=True, simulator=sim)
    yield d
    d.close()
