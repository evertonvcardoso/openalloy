# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""openalloyd - the user-level lighting daemon.

Runs as ``systemctl --user`` service. Owns the keyboard, streams live effects,
reconnects on replug, and serves the GUI/CLI over a 0600 Unix socket.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from openalloy import __version__, i18n
from openalloy.application.backend import make_handlers
from openalloy.application.engine import Engine
from openalloy.config.settings import PresetStore, Settings, socket_path
from openalloy.i18n import _
from openalloy.infra.drivers import open_driver
from openalloy.infra.logging_config import setup_logging
from openalloy.infra.rpc import RpcError, RpcServer

log = logging.getLogger("openalloyd")


def main(argv=None) -> int:
    i18n.set_language(i18n.resolve())
    i18n.install_argparse()
    ap = argparse.ArgumentParser(prog="openalloyd", description=_("OpenAlloy service"))
    ap.add_argument("--simulate", action="store_const", const="core",
                    help=_("use a simulated keyboard (Kingston, fw 21.08)"))
    ap.add_argument("--simulate-hp", dest="simulate", action="store_const", const="hp",
                    help=_("simulate the HP 03f0:098f fw 21.16 (HID LampArray only)"))
    ap.add_argument("--sim-layout", choices=["ansi", "abnt2", "iso"],
                    help=_("layout of the simulated keyboard: ansi (US), abnt2 (default) or iso"))
    ap.add_argument("--socket", help=_("socket path (default: $XDG_RUNTIME_DIR)"))
    ap.add_argument("-v", "--verbose", action="store_true", help=_("more detailed log"))
    args = ap.parse_args(argv)
    if args.sim_layout:
        import os
        os.environ["OPENALLOY_SIM_LAYOUT"] = args.sim_layout
    setup_logging(daemon=True, verbose=args.verbose)

    simulator = None
    if args.simulate == "hp":
        from openalloy.infra.simulator import SimulatedLampArray
        simulator = SimulatedLampArray()
    elif args.simulate:
        from openalloy.infra.simulator import SimulatedKeyboard
        simulator = SimulatedKeyboard()

    settings = Settings()
    engine = Engine(lambda: open_driver(args.simulate, simulator), settings, PresetStore())
    server = RpcServer(args.socket or socket_path(), make_handlers(engine))
    engine.subscribe(server.broadcast)
    # Installed before the socket exists, so an early stop is never lost. Signal
    # handlers run in the main thread between two bytecodes - possibly while it
    # holds a lock (an Event.set() there deadlocked against Event.wait()). So they take
    # no lock at all: a stop only flips a flag, a reload is handed to a thread.
    stopping: list = []

    def request_stop(*_args):
        stopping.append(True)

    def do_reload():
        settings.reload()
        i18n.set_language(i18n.resolve())
        engine.reconfigure()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, lambda *_args: threading.Thread(
        target=do_reload, name="openalloy-reload", daemon=True).start())
    try:
        server.start()
    except RpcError as exc:
        log.error("%s", exc)
        return 1

    log.info("OpenAlloy service %s started (mode %s, language %s)", __version__, engine.mode,
             i18n.current())
    engine.start()
    try:
        # A short sleep, never an endless wait: after a freeze/thaw the kernel may hand
        # SIGTERM to another thread, and the main thread must still get to run it.
        while not stopping:
            time.sleep(0.5)
    finally:
        log.info("Stopping the service")
        engine.stop()
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
