# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""The lighting engine: single owner of the keyboard.

Evolution of Linuity's Daemon loop (detect -> connect -> run effect -> sleep,
reset on disconnect) with the differences a per-key RGB keyboard needs:

* frames are rendered from a pure effect function and streamed with a
  keepalive (the Core reverts to its onboard profile a few seconds after the
  live stream stops);
* changes are applied instantly through method calls (RPC from the GUI/CLI)
  instead of restarting a systemd service through sudo;
* onboard-flash operations pause streaming and run under the device lock;
* firmware notifications (Fn + profile keys) are honoured.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from openalloy.application import flashgate
from openalloy.application.effects import Lighting, Renderer, bake, normalize
from openalloy.application.ports import KeyboardDriver
from openalloy.config.settings import PresetStore, Settings
from openalloy.domain.colors import DEFAULT_LED_GAMMA, RGB, led_correct, parse_color, to_hex
from openalloy.domain.layout import INDEX_BY_HID_USAGE, VARIANT_ANSI, Layout, get_layout
from openalloy.i18n import N_, Message, _
from openalloy.infra import protocol as P
from openalloy.infra.transport import PermissionDenied

log = logging.getLogger(__name__)

STATIC_KEEPALIVE = 0.05   # OpenRGB resends after 50 ms; hxoctkl uses 10 ms
# Identical frames are not resent more often than this (idea from
# hyperx-studio); still far below the few seconds after which the Core
# falls back to its onboard profile.
HOLD_REFRESH = 0.25
RETRY_DELAY = 2.0
# Sleep longer than this (seen as CLOCK_BOOTTIME pulling ahead of CLOCK_MONOTONIC) is
# a system suspend; a sleep notice older than STALE_NOTICE was delivered after the
# resume (the systemd hook could not reach a frozen service) and is ignored.
RESUME_GAP = 2.0
STALE_NOTICE = 2.0


def boottime() -> float:
    """Seconds since boot, *including* time suspended (same value in every process)."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clock) if clock is not None else time.monotonic()


def _sleep_gap() -> float:
    """Total time the system spent suspended so far (grows only across a suspend)."""
    return boottime() - time.monotonic()


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, opener: Callable[[], KeyboardDriver], settings: Settings,
                 presets: Optional[PresetStore] = None, enable_input: bool = True,
                 presence: Optional[Callable[[], tuple]] = None,
                 sleep_gap: Optional[Callable[[], float]] = None):
        self._opener = opener
        self.settings = settings
        self.presets = presets or PresetStore()
        self._enable_input = enable_input
        self._dev: Optional[KeyboardDriver] = None
        self._dev_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = 0                    # >0 while a flash operation runs
        self._busy_lock = threading.Lock()
        self._force_send = False          # next frame goes out even if unchanged
        # (English template, parameters): translated when read, so a language switch
        # also changes an error that is already on screen.
        self._error: Optional[Message] = None
        self._info: Optional[dict] = None
        self._listeners: List[Callable[[str, dict], None]] = []
        self._last_frame: List[RGB] = [(0, 0, 0)] * 94
        self._t0 = time.monotonic()
        self._frames_sent = 0
        self._input = None
        self._audio = None
        self._audio_retry = 0.0
        self._asleep = False
        # "fixed" mode: apply once, let go of the device, re-apply only when the
        # keyboard reappears (replug) or the PC wakes up.
        self._fixed_applied = False
        self._fixed_sig: Optional[tuple] = None
        self._fixed_gone = False          # keyboard vanished since the colour was set
        self._presence = presence or _hid_presence
        self._onboard_sig: Optional[tuple] = None
        self._onboard_checked = 0.0
        self._sleep_gap = sleep_gap or _sleep_gap
        self._gap = self._sleep_gap()
        self._sleep_lock = threading.Lock()   # suspend/wake/resume never interleave
        self._last_brightness: Optional[int] = None

        self.mode = settings.get("mode", "live")
        if self.mode not in ("live", "onboard", "fixed"):
            self.mode = "live"
        self.layout: Layout = get_layout(settings.get("layout_override") or VARIANT_ANSI)
        self._renderer = self._make_renderer(Lighting.from_dict(settings.get("lighting")))

    # --- events ----------------------------------------------------------
    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, kind: str, data: Optional[dict] = None) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, data or {})
            except Exception:
                log.exception("listener failed")

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openalloy-engine", daemon=True)
        self._thread.start()
        self._update_input()

    def stop(self, release: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._input:
            self._input.stop()
        if self._audio:
            self._audio.stop()
        self._disconnect()

    def _disconnect(self) -> None:
        with self._dev_lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
            self._dev = None
            self._info = None

    def _connect(self) -> Optional[KeyboardDriver]:
        if self._asleep:
            return None
        with self._dev_lock:
            if self._dev is not None:
                return self._dev
            try:
                dev = self._opener()
                info = dev.info()
            except PermissionDenied as exc:
                self._set_error(N_("{error}. Install the udev rule (install.sh) and replug the "
                                   "keyboard."), error=exc)
                return None
            except Exception as exc:
                from openalloy.infra.transport import DeviceNotFound
                if isinstance(exc, DeviceNotFound):
                    self._set_error(N_("Keyboard not detected: {error}"), error=exc)
                else:
                    self._set_error(N_("Keyboard found, but communication failed: {error}"),
                                    error=exc)
                return None
            dev.set_notify_handler(self._on_notify)
            self._dev = dev
            self._info = info.to_dict()
            override = self.settings.get("layout_override")
            self.layout = get_layout(override or info.variant or VARIANT_ANSI)
            with self._state_lock:
                self._renderer = self._make_renderer(self._renderer.lighting)
            restore = self.settings.get("restore_brightness")
            if restore is None and info.driver == "hid_lamparray":
                # LampArray has no hardware brightness: the level lives with us.
                restore = (self._last_brightness if self._last_brightness is not None
                           else self.settings.get("software_brightness"))
            if restore is not None:
                try:
                    dev.set_brightness(int(restore))
                except Exception:
                    log.warning("could not restore brightness")
            self._set_error(None)
            log.info("Connected: %s (firmware %s, layout %s) on %s", info.name, info.firmware,
                     self.layout.variant, info.path)
            self._emit("connected", self._info)
            return dev

    def _set_error(self, msg: Optional[str], **params) -> None:
        err = Message(msg, **params) if msg else None
        if err != self._error:
            self._error = err
            if err:
                log.warning("%s", err)          # English in the journal (logging_config)
            self._emit("status", self.status())

    def error_text(self) -> Optional[str]:
        """The current error in the interface language (rendered now, so a language
        switch also changes an error that is already on screen)."""
        return str(self._error) if self._error else None

    def _make_renderer(self, lighting: Lighting) -> Renderer:
        try:
            return Renderer(normalize(lighting, self.layout), self.layout)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("Invalid lighting (%s); using the default", exc)
            return Renderer(Lighting("wave"), self.layout)

    # --- main loop -------------------------------------------------------
    def _run(self) -> None:
        last_sent = 0.0          # last tick
        last_written = 0.0       # last time a frame actually reached the device
        written_to = None        # device the last frame went to
        while not self._stop.is_set():
            self._check_resume()
            if self._asleep:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            if self.mode == "fixed" and self._fixed_tick():
                continue
            dev = self._connect()
            if dev is None:
                self._wake.wait(RETRY_DELAY)
                self._wake.clear()
                continue
            try:
                with self._dev_lock:
                    dev.poll(0.0)
                if self._busy or self.mode not in ("live", "fixed"):
                    if self.mode == "onboard" and not self._busy and self._onboard_replugged():
                        continue                     # stale handle dropped: reconnect
                    self._wake.wait(0.1)
                    self._wake.clear()
                    continue
                now = time.monotonic()
                with self._state_lock:
                    renderer = self._renderer
                animated = renderer.animated or renderer.effect.live_only
                try:
                    fps = max(5, min(60, int(self.settings.get("fps", 30))))
                except (TypeError, ValueError):
                    fps = 30
                interval = 1.0 / fps if animated else STATIC_KEEPALIVE
                if now - last_sent >= interval:
                    if renderer.needs_audio and self._audio is not None:
                        if not self._audio.running and now - self._audio_retry > 5.0:
                            self._audio_retry = now      # recorder died: try again
                            self._audio.start()
                        renderer.set_sound(self._audio.frame())
                    frame = renderer.frame(now - self._t0)
                    unchanged = (frame == self._last_frame and written_to is dev
                                 and now - last_written < getattr(dev, "KEEPALIVE",
                                                                  HOLD_REFRESH)
                                 and not self._force_send)
                    if not unchanged:
                        with self._dev_lock:
                            if (not self._busy and self.mode in ("live", "fixed")
                                    and not self._asleep):
                                self._force_send = False
                                dev.stream(self._led(frame))
                                self._frames_sent += 1
                                last_written, written_to = now, dev
                    self._last_frame = frame
                    last_sent = now
                sleep = max(0.0, interval - (time.monotonic() - last_sent))
                self._wake.wait(min(sleep, 0.05))
                self._wake.clear()
            except Exception as exc:
                log.warning("Communication error (%s); reconnecting", exc)
                self._disconnect()
                self._set_error(N_("Connection lost: {error}"), error=exc)
                self._emit("disconnected", {})
                self._stop.wait(1.0)

    def resend(self) -> None:
        """Send the current lighting again (e.g. after the colour correction changed)."""
        self._force_send = True
        self._fixed_applied = False
        self._wake.set()

    def reconfigure(self) -> None:
        """Re-read layout-related settings and rebuild the lighting, keeping the mode."""
        override = self.settings.get("layout_override")
        variant = override or (self._info or {}).get("variant") or VARIANT_ANSI
        self.layout = get_layout(variant)
        with self._state_lock:
            self._renderer = self._make_renderer(self._renderer.lighting)
            lighting = self._renderer.lighting.to_dict()
        self._update_input()
        self.resend()
        self._emit("lighting", lighting)
        self._emit("status", self.status())

    def _led(self, frame):
        """Screen colours -> LED drive levels (see domain.colors.led_correct)."""
        return led_correct(frame, self.settings.get("led_gamma", DEFAULT_LED_GAMMA))

    def _fixed_tick(self) -> bool:
        """Handle fixed mode. Returns False to fall back to live streaming
        (drivers whose firmware does not keep the colours on its own)."""
        if self._fixed_applied:
            sig = self._presence()
            if not sig:
                self._fixed_gone = True          # unplugged (or being reset)
            elif sig != self._fixed_sig or self._fixed_gone:
                log.info("Keyboard reconnected: re-applying the fixed color")
                self._fixed_applied = False
                self._fixed_gone = False
                self._disconnect()               # any handle we kept is stale now
            if self._fixed_applied:
                self._wake.wait(1.0)
                self._wake.clear()
                return True
        dev = self._connect()
        if dev is None:
            self._wake.wait(RETRY_DELAY)
            self._wake.clear()
            return True
        detach = getattr(dev, "detach", None)
        if detach is None:
            return False            # vendor channel: keep streaming instead
        try:
            with self._state_lock:
                renderer = self._renderer
            frame = renderer.frame(0.0)
            with self._dev_lock:
                dev.stream(self._led(frame))
                time.sleep(0.15)
                dev.stream(self._led(frame))
                detach()
                self._dev = None            # keep self._info for the status
            self._last_frame = frame
            self._frames_sent += 2
            self._fixed_applied = True
            self._fixed_gone = False
            self._fixed_sig = self._presence()
            log.info("Fixed color applied; the keyboard keeps it with nothing streaming")
            self._emit("mode", {"mode": "fixed", "applied": True})
        except Exception as exc:
            log.warning("Could not fix the color (%s); retrying", exc)
            self._disconnect()
            self._wake.wait(1.0)
            self._wake.clear()
        return True

    def _busy_add(self, n: int) -> None:
        with self._busy_lock:
            self._busy += n

    # --- notifications & input --------------------------------------------
    def _on_notify(self, kind: str, data: dict) -> None:
        if kind == "profile_key":
            profile = data.get("profile")
            log.info("Profile key %s pressed on the keyboard", profile)
            self._emit("profile_key", data)
            action = self.settings.get("profile_key_action", "release")
            if action == "release" and self.mode == "live":
                self._set_mode("onboard", persist=True)
            elif action == "bind":
                bound = (self.settings.get("profile_bindings") or {}).get(str(profile))
                preset = self.presets.get(bound) if bound else None
                if preset:
                    threading.Thread(target=self.apply, args=(preset["lighting"], True),
                                     daemon=True).start()
                elif self.mode == "live":
                    self._set_mode("onboard", persist=True)
        elif kind == "key":
            idx = INDEX_BY_HID_USAGE.get(data.get("usage"))
            if idx is not None and data.get("pressed"):
                self.key_pressed(idx)

    def key_pressed(self, index: int) -> None:
        with self._state_lock:
            self._renderer.on_key(index, time.monotonic() - self._t0)
        self._wake.set()

    def _on_evdev(self, code: int) -> None:
        k = self.layout.by_evdev(code)
        if k:
            self.key_pressed(k.index)

    def _update_input(self) -> None:
        self._update_audio()
        wants = (self._enable_input and self.settings.get("reactive_input", True)
                 and self._renderer.wants_keys and self.mode == "live" and not self._asleep)
        if wants:
            if self._input is None:
                from openalloy.infra.input import EvdevKeySource
                self._input = EvdevKeySource(P.SUPPORTED_IDS, self._on_evdev)
            self._input.start()
        elif self._input is not None:
            self._input.stop()

    def _update_audio(self) -> None:
        wants = (self._enable_input and self._renderer.needs_audio and self.mode == "live"
                 and not self._asleep)
        if wants:
            if self._audio is None:
                from openalloy.infra.audio import AudioCapture
                self._audio = AudioCapture(self.settings.get("audio_source") or "")
            if not self._audio.start():
                log.warning("Sound effect without audio: %s", self._audio.error)
        elif self._audio is not None:
            self._audio.stop()
            self._audio = None

    def restart_audio(self) -> None:
        """Pick up a new audio source."""
        if self._audio is not None:
            self._audio.stop()
            self._audio = None
        self._update_audio()

    def sound_frame(self) -> dict:
        if self._audio is None:
            return {"on": False, "level": 0, "beat": 0, "bands": [0] * 12,
                    "error": None if not self._renderer.needs_audio else _("capture inactive")}
        d = self._audio.frame().to_dict()
        d["error"] = str(self._audio.error) if self._audio.error else None
        return d

    # --- system sleep (idea from hyperx-studio) ----------------------------
    def suspend(self, sent_at: Optional[float] = None) -> None:
        """Release the keyboard before the machine sleeps.

        A keyboard held in direct mode with no frames arriving may stop
        signalling USB remote wake-up (observed by hyperx-studio on the
        full-size Alloy Origins). The sleep hook calls this, then resets the
        USB device so the firmware restarts on its onboard profile.

        ``sent_at`` is the hook's boottime(): systemd 256+ freezes user services while
        the hook runs, so its notice may only arrive after the resume - possibly
        after the matching "wake". Such a late notice is ignored instead of putting
        the service to sleep with the PC already awake.
        """
        if sent_at is not None:
            try:
                late = boottime() - float(sent_at) > STALE_NOTICE
            except (TypeError, ValueError):
                late = False
            if late:
                log.info("Sleep notice arrived after the system resumed; ignored")
                return
        with self._sleep_lock:
            if self._asleep:
                return
            self._asleep = True
            self._wake.set()
            with self._dev_lock:          # waits for an in-flight frame
                pass
            if self._input:
                self._input.stop()
            self._update_audio()
            self._disconnect()
        log.info("System going to sleep: keyboard released")
        self._emit("mode", {"mode": self.mode, "asleep": True})

    def wake(self, sent_at: Optional[float] = None) -> None:      # noqa: ARG002 - hook API
        with self._sleep_lock:
            if not self._asleep:
                return
            self._gap = self._sleep_gap()     # this resume is handled: no second take-back
            self._take_back()
        log.info("System resumed: taking the keyboard back")
        self._emit("mode", {"mode": self.mode, "asleep": False})

    def _check_resume(self) -> None:
        """Notice a suspend/resume by the clocks, without the hook: the hook cannot reach
        the service while systemd keeps user services frozen (systemd 256+), and a
        handle kept across the USB reset done by the hook is stale anyway."""
        if self._sleep_gap() - self._gap <= RESUME_GAP:
            return                            # the common case: no lock taken
        with self._sleep_lock:
            gap = self._sleep_gap()           # again under the lock: wake() may have just
            slept = gap - self._gap           # handled this very resume
            if slept <= RESUME_GAP:
                return
            self._gap = gap
            if self._input:
                self._input.stop()        # its event nodes may be gone too
            self._disconnect()
            self._take_back()
        log.info("System resumed (%.0f s asleep): taking the keyboard back", slept)
        self._emit("mode", {"mode": self.mode, "asleep": False})

    def _onboard_replugged(self) -> bool:
        """In onboard mode nothing is sent, so an unplug would go unnoticed (LampArray
        polls nothing) and the status would keep showing a dead handle. Watch the
        device's identity instead - once a second, as fixed mode does."""
        now = time.monotonic()
        if now - self._onboard_checked < 1.0:
            return False
        self._onboard_checked = now
        sig = self._presence()
        if self._onboard_sig is None:
            self._onboard_sig = sig
            return False
        if sig == self._onboard_sig:
            return False
        self._onboard_sig = None
        log.info("Keyboard reconnected (onboard mode)")
        self._disconnect()
        if not sig:
            self._set_error(N_("Keyboard not detected: {error}"),
                            error=Message(N_("HyperX Alloy Origins Core not found")))
            self._emit("disconnected", {})
        return True

    def _take_back(self) -> None:
        self._asleep = False
        self._fixed_applied = False         # the keyboard lost the colours while asleep
        self._force_send = True
        self._update_input()
        self._wake.set()

    # --- public API (used by RPC and the embedded GUI backend) -----------
    def status(self) -> dict:
        with self._state_lock:
            lighting = self._renderer.lighting.to_dict()
        return {
            "connected": self._dev is not None or (self.mode == "fixed" and self._fixed_applied),
            "device": self._info,
            "mode": self.mode,
            "busy": bool(self._busy),
            "error": self.error_text(),
            "layout": self.layout.variant,
            "lighting": lighting,
            "frames_sent": self._frames_sent,
            "input": self._input.status if self._input else _("off"),
            "audio": (str(self._audio.error or _("on"))) if self._audio else _("off"),
            "asleep": self._asleep,
            "fixed_applied": self.mode == "fixed" and self._fixed_applied,
            "flash": self._flash_check()[0],
        }

    def _require(self) -> KeyboardDriver:
        dev = self._connect()
        if dev is None:
            if self._asleep:
                raise EngineError(Message(N_("the system is asleep; the keyboard was released")))
            raise EngineError(self._error or Message(N_("keyboard not connected")))
        return dev

    def apply(self, lighting: Dict[str, Any], persist: bool = True) -> dict:
        light = normalize(Lighting.from_dict(lighting), self.layout)
        with self._state_lock:
            self._renderer = Renderer(light, self.layout)
        self._set_mode("live", persist=False)
        if persist:
            self.settings.update(lighting=light.to_dict(), mode="live")
        self._update_input()
        self._wake.set()
        self._emit("lighting", light.to_dict())
        return light.to_dict()

    def _set_mode(self, mode: str, persist: bool) -> None:
        previous = self.mode
        self.mode = mode
        self._onboard_sig = None
        if mode == "fixed":
            self._fixed_applied = False
        dev = self._dev
        if dev is None and mode == "onboard" and previous == "fixed" and not self._asleep:
            # Fixed mode let go of the device on purpose, but the colour it set is still
            # held by the keyboard: open it again, or "release" would change nothing.
            dev = self._connect()
        if dev is not None and mode == "onboard":
            try:
                with self._dev_lock:
                    dev.release_control()      # LampArray: back to firmware lighting
            except Exception as exc:
                log.warning("Could not hand control back to the firmware: %s", exc)
        if persist:
            self.settings.update(mode=mode)
        self._update_input()
        self._wake.set()
        self._emit("mode", {"mode": mode})

    def fix(self, lighting: Optional[Dict[str, Any]] = None, persist: bool = True) -> dict:
        """Apply the lighting once and keep it on the keyboard with nothing streaming
        (HID LampArray). Falls back to live streaming on the vendor channel."""
        light = normalize(Lighting.from_dict(lighting) if lighting is not None
                          else self._renderer.lighting, self.layout)
        light.reaction = {}
        with self._state_lock:
            self._renderer = Renderer(light, self.layout)
        self._set_mode("fixed", persist=False)
        if persist:
            self.settings.update(lighting=light.to_dict(), mode="fixed")
        self._wake.set()
        self._emit("lighting", light.to_dict())
        return light.to_dict()

    def release(self, persist: bool = True) -> None:
        """Stop streaming; the keyboard falls back to its onboard profile."""
        self._set_mode("onboard", persist)

    def resume(self) -> None:
        self._set_mode("live", persist=True)

    def set_brightness(self, level: int, persist: bool = True) -> int:
        dev = self._require()
        with self._dev_lock:
            dev.set_brightness(level)
            got = dev.get_brightness()
        self._last_brightness = int(level)
        if (self._info or {}).get("driver") == "hid_lamparray" and persist:
            self.settings.update(software_brightness=max(0, min(255, int(level))))
        if self.mode == "fixed":
            self._fixed_applied = False          # re-apply with the new brightness
            self._wake.set()
        # Only remember the level when the user opted into restoring it.
        if persist and self.settings.get("restore_brightness") is not None:
            self.settings.update(restore_brightness=int(level))
        return got if got is not None else int(level)

    def get_brightness(self) -> Optional[int]:
        dev = self._require()
        with self._dev_lock:
            return dev.get_brightness()

    _NO_ONBOARD = N_("This keyboard/firmware does not accept the vendor memory channel; it is "
                     "driven through the HID LampArray standard (Dynamic Lighting), which "
                     "allows live colors but no profiles and no writing to memory.")

    def _need(self, dev: KeyboardDriver, what: str) -> None:
        caps = dev.capabilities
        ok = {"profiles": caps.profiles > 0, "onboard": caps.onboard_animation,
              "keypress": caps.onboard_keypress}[what]
        if not ok:
            raise EngineError(Message(self._NO_ONBOARD))

    def set_profile(self, profile: int) -> int:
        dev = self._require()
        self._need(dev, "profiles")
        with self._dev_lock:
            dev.set_profile(profile)
            got = dev.get_profile()
        return got if got is not None else profile

    def get_profile(self) -> Optional[int]:
        dev = self._require()
        with self._dev_lock:
            return dev.get_profile()

    def device_state(self) -> dict:
        dev = self._require()
        with self._dev_lock:
            return {"brightness": dev.get_brightness(), "profile": dev.get_profile(),
                    "info": dev.info().to_dict()}

    def read_onboard(self, profile: int) -> List[str]:
        dev = self._require()
        self._need(dev, "keypress")
        self._busy_add(1)
        try:
            with self._dev_lock:
                return [to_hex(c) for c in dev.read_keypress_bank(profile)]
        finally:
            self._busy_add(-1)

    def upload_onboard(self, profile: int, lighting: Optional[Dict[str, Any]] = None,
                       frames: Optional[int] = None, keypress: Optional[str] = None,
                       keypress_color: Optional[str] = None,
                       progress: Optional[Callable[[float, str], None]] = None) -> dict:
        """Bake an effect and write it to onboard profile 1-3 (persists in flash)."""
        self._need(self._require(), "onboard")
        allowed, reason = self._flash_check()
        if not allowed:
            raise EngineError(Message(N_("Writing to memory is blocked: {reason}."), reason=reason))
        profile = P.check_profile(profile)
        light = normalize(Lighting.from_dict(lighting) if lighting is not None
                          else self._renderer.lighting, self.layout)
        frames = int(frames or self.settings.get("onboard_frames", 60))
        keypress = keypress or self.settings.get("onboard_keypress", "match")
        if keypress not in ("match", "black", "keep", "custom"):
            raise EngineError(Message(N_("keypress must be match, black, keep or custom")))
        baked = [self._led(f) for f in bake(light, self.layout, frames)]
        dev = self._require()
        report = progress or (lambda f, m: None)
        self._busy_add(1)
        try:
            with self._dev_lock:
                peak = dev.upload_animation(profile, baked,
                                            lambda f, m: report(f * 0.9, m))
                verified = None
                if keypress != "keep":
                    if keypress == "match":
                        layer = baked[0]
                    elif keypress == "black":
                        layer = [(0, 0, 0)] * 94
                    else:
                        layer = [parse_color(keypress_color or "#ffffff")] * 94
                    report(0.95, _("writing the key-press layer"))
                    verified = dev.write_keypress_bank(profile, layer)
                dev.set_profile(profile)
        finally:
            self._busy_add(-1)
        report(1.0, _("done"))
        # The upload selected the profile on the keyboard; show it.
        self._set_mode("onboard", persist=True)
        result = {"profile": profile, "frames": len(baked), "peak": to_hex(peak),
                  "keypress": keypress, "keypress_verified": verified,
                  "lighting": light.to_dict()}
        log.info("Onboard profile %d written: %s (%d frames)", profile, light.effect, len(baked))
        self._emit("onboard_written", result)
        return result

    # --- flash gate ---------------------------------------------------------
    def _flash_check(self):
        caps = (self._info or {}).get("capabilities") or {}
        if self._info and not caps.get("onboard_animation"):
            return False, _("unavailable: this firmware only accepts the HID LampArray "
                            "standard (live colors), without memory access")
        return flashgate.check(self._info, self.settings.get("flash_validated") or {})

    def flash_status(self) -> dict:
        allowed, reason = self._flash_check()
        caps = (self._info or {}).get("capabilities") or {}
        return {"allowed": allowed, "reason": reason, "device": flashgate.device_key(self._info),
                "supported": bool(caps.get("onboard_animation")) or not self._info,
                "testing": self._live_test_backup is not None}

    _live_test_backup: Optional[dict] = None

    def live_test(self) -> dict:
        """Show a known pattern live so the user can confirm RGB really works."""
        self._require()
        if self._live_test_backup is None:
            with self._state_lock:
                self._live_test_backup = {"lighting": self._renderer.lighting.to_dict(),
                                          "mode": self.mode}
        pattern = flashgate.test_pattern(self.layout)
        self.apply(pattern, persist=False)
        log.info("Live RGB test shown; waiting for the user to confirm")
        return {"lighting": pattern, "expected": _(flashgate.TEST_DESCRIPTION)}

    def confirm_live_test(self, ok: bool) -> dict:
        """Restore the previous lighting; if confirmed, unlock flash for this unit."""
        backup, self._live_test_backup = self._live_test_backup, None
        if backup:
            if backup["mode"] == "fixed":
                self.fix(backup["lighting"], persist=False)
            else:
                self.apply(backup["lighting"], persist=False)
                if backup["mode"] == "onboard":
                    self.release(persist=False)
        if ok and self._info and not self._info.get("simulated"):
            key, rec = flashgate.record(self._info)
            validated = dict(self.settings.get("flash_validated") or {})
            validated[key] = rec
            self.settings.update(flash_validated=validated)
            log.info("Live RGB confirmed on %s; writing to memory unlocked", key)
        elif not ok:
            log.warning("The user reported live RGB did NOT work; writing stays blocked")
        status = self.flash_status()
        self._emit("flash", status)
        return status

    def preview_frame(self) -> List[str]:
        return [to_hex(c) for c in self._last_frame]


def _hid_presence() -> tuple:
    """Identity of the connected supported keyboards (changes on replug)."""
    try:
        from openalloy.infra.transport import enumerate_hidraw
        # sysfs paths carry the HID instance number, which changes on every
        # enumeration - a replug is noticed even if /dev/hidrawN is reused.
        return tuple(sorted((n.path, n.sysfs) for n in enumerate_hidraw()
                            if (n.vendor_id, n.product_id) in P.SUPPORTED_IDS))
    except Exception:
        return ()
