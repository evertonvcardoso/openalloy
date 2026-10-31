# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""One API, two transports.

CLI and GUI call ``backend.call("method", **params)``. If the user daemon is
running the call goes over RPC (the daemon owns the keyboard); otherwise an
in-process Engine is started, so everything also works with no service
installed (live effects then last while the GUI stays open).
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Callable, Dict, Optional

from openalloy import i18n
from openalloy.application.effects import Lighting, describe_effects, describe_reaction, normalize
from openalloy.application.engine import Engine
from openalloy.config.settings import DEFAULTS, PresetStore, Settings, socket_path
from openalloy.i18n import N_, Message
from openalloy.infra.rpc import RpcClient, RpcError, is_alive, with_progress

log = logging.getLogger(__name__)

SETTABLE = {"language", "fps", "layout_override", "profile_key_action", "profile_bindings",
            "reactive_input", "restore_brightness", "onboard_frames", "onboard_keypress",
            "onboard_keypress_color", "audio_source", "led_gamma", "software_brightness"}


def make_handlers(engine: Engine) -> Dict[str, Callable[..., Any]]:
    presets = engine.presets

    def settings_update(**values):
        bad = set(values) - SETTABLE
        if bad:
            raise ValueError(Message(N_("keys not allowed: {keys}"), keys=", ".join(sorted(bad))))
        engine.settings.update(**values)          # validates every value first
        if "language" in values:                  # the service follows the window/CLI
            i18n.set_language(engine.settings.get("language"))
        if "layout_override" in values or "reactive_input" in values:
            engine.reconfigure()                  # keeps the current mode
        elif "led_gamma" in values:
            engine.resend()
        if "audio_source" in values:
            engine.restart_audio()
        return engine.settings.snapshot()

    def preset_save(name, lighting):
        light = normalize(Lighting.from_dict(lighting), engine.layout)
        return presets.save(name, light.to_dict())

    def preset_apply(ident: str, persist: bool = True):
        p = presets.get(ident)
        if not p:
            raise ValueError(Message(N_("preset not found: {name}"), name=ident))
        return engine.apply(p["lighting"], persist)

    def upload_onboard(profile, lighting=None, frames=None, keypress=None,
                       keypress_color=None, progress=None):
        return engine.upload_onboard(int(profile), lighting, frames, keypress,
                                     keypress_color, progress)

    return {
        "ping": lambda: "pong",
        "status": engine.status,
        "effects": describe_effects,
        "reaction_params": describe_reaction,
        "sound_frame": engine.sound_frame,
        "suspend": engine.suspend,
        "flash_status": engine.flash_status,
        "live_test": engine.live_test,
        "confirm_live_test": lambda ok: engine.confirm_live_test(bool(ok)),
        "wake": engine.wake,
        "apply": lambda lighting, persist=True: engine.apply(lighting, persist),
        "release": lambda persist=True: engine.release(persist),
        "fix": lambda lighting=None, persist=True: engine.fix(lighting, persist),
        "resume": engine.resume,
        "set_brightness": lambda level, persist=True: engine.set_brightness(int(level), persist),
        "get_brightness": engine.get_brightness,
        "set_profile": lambda profile: engine.set_profile(int(profile)),
        "get_profile": engine.get_profile,
        "device_state": engine.device_state,
        "read_onboard": lambda profile: engine.read_onboard(int(profile)),
        "upload_onboard": with_progress(upload_onboard),
        "preview_frame": engine.preview_frame,
        "key_pressed": lambda index: engine.key_pressed(int(index)),
        "settings_get": engine.settings.snapshot,
        "settings_defaults": lambda: copy.deepcopy(DEFAULTS),
        "settings_update": settings_update,
        "presets_list": presets.list,
        "preset_save": preset_save,
        "preset_delete": lambda ident: presets.delete(ident),
        "preset_apply": preset_apply,
    }


class Backend:
    kind = "?"

    def call(self, method: str, progress: Optional[Callable[[float, str], None]] = None,
             **params) -> Any:
        raise NotImplementedError

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        pass

    def close(self) -> None:
        pass


class RemoteBackend(Backend):
    kind = "daemon"

    def __init__(self, path=None):
        self.client = RpcClient(path or socket_path())
        self._stop = threading.Event()

    def call(self, method, progress=None, **params):
        timeout = 600.0 if method in ("upload_onboard", "read_onboard") else 15.0
        return self.client.call(method, progress=progress, timeout=timeout, **params)

    def subscribe(self, fn):
        self.client.subscribe(fn, self._stop)

    def close(self):
        self._stop.set()


class LocalBackend(Backend):
    kind = "local"

    def __init__(self, simulate: bool = False, settings: Optional[Settings] = None,
                 start: bool = True, simulator=None):
        from openalloy.infra.drivers import open_driver
        self.simulate = simulate
        self.simulator = simulator
        if simulate == "hp" and simulator is None:
            from openalloy.infra.simulator import SimulatedLampArray
            self.simulator = SimulatedLampArray()
        elif simulate and simulator is None:
            from openalloy.infra.simulator import SimulatedKeyboard
            self.simulator = SimulatedKeyboard()
        self.engine = Engine(lambda: open_driver(simulate, self.simulator),
                             settings or Settings(), PresetStore())
        self.handlers = make_handlers(self.engine)
        if start:
            self.engine.start()

    def call(self, method, progress=None, **params):
        fn = self.handlers.get(method)
        if fn is None:
            raise RpcError(Message(N_("unknown method: {method}"), method=method))
        if getattr(fn, "wants_progress", False):
            params["progress"] = progress
        return fn(**params)

    def subscribe(self, fn):
        self.engine.subscribe(fn)

    def close(self):
        self.engine.stop()


def connect(simulate: bool = False, allow_local: bool = True, start_local: bool = True
            ) -> Backend:
    """Prefer the running daemon; fall back to an in-process engine."""
    if not simulate:
        path = socket_path()
        if path.exists() and is_alive(path):
            return RemoteBackend(path)
    if not allow_local:
        raise RpcError(Message(N_("the service is not running (systemctl --user start openalloy)")))
    return LocalBackend(simulate=simulate, start=start_local)
