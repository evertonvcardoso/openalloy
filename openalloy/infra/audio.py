# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""System audio capture for sound-reactive effects.

Approach adapted from hyperx-studio (MIT, internal/audio/audio.go): the
stream comes from PipeWire/PulseAudio's own recorder (``parec`` or
``pw-record``) reading the monitor of the default output, 12 bands are
measured with the Goertzel algorithm, with auto-gain on a log scale and a
beat detector that compares bass with its running average. Standard library
only; if no recorder exists the effects show a gentle idle animation.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import threading
from typing import List, Optional

from openalloy.domain.sound import BANDS, Sound
from openalloy.i18n import N_, Message

log = logging.getLogger(__name__)

SAMPLE_RATE = 22050
BLOCK = 1024                       # ~46 ms
BAND_HZ = (60, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 9000)
assert len(BAND_HZ) == BANDS

_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (BLOCK - 1)) for i in range(BLOCK)]
_COEFF = [2 * math.cos(2 * math.pi * round(hz * BLOCK / SAMPLE_RATE) / BLOCK) for hz in BAND_HZ]


def recorder() -> Optional[List[str]]:
    if shutil.which("parec"):
        return ["parec", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1",
                "--latency-msec=25"]
    if shutil.which("pw-record"):
        return ["pw-record", f"--rate={SAMPLE_RATE}", "--channels=1", "--format=s16"]
    return None


def default_monitor() -> str:
    if not shutil.which("pactl"):
        return ""
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{sink}.monitor" if sink else ""


def goertzel(x: List[float], coeff: float) -> float:
    s1 = s2 = 0.0
    for v in x:
        s0 = v + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return math.sqrt(max(0.0, power)) / len(x) * 2


def _decay_peak(peak: float, v: float) -> float:
    peak = max(peak * 0.995, v)
    return max(peak, 0.004)


def _norm(v: float, peak: float) -> float:
    if v <= 0 or peak <= 0:
        return 0.0
    return max(0.0, min(1.0, 1 + 20 * math.log10(v / peak) / 28))


def _smooth(prev: float, now: float) -> float:
    return prev + (now - prev) * (0.6 if now > prev else 0.18)


class Analyzer:
    """Turns blocks of samples into a Sound (pure; unit-testable)."""

    def __init__(self):
        self.sound = Sound(on=True)
        self._peak = 0.0
        self._band_peaks = [0.0] * BANDS
        self._bass_avg = 0.0

    def process(self, samples: List[float]) -> Sound:
        n = len(samples)
        rms = math.sqrt(sum(v * v for v in samples) / n) if n else 0.0
        win = [v * w for v, w in zip(samples, _HANN)] if n == BLOCK else samples
        raw = [goertzel(win, c) for c in _COEFF]
        s = self.sound
        self._peak = _decay_peak(self._peak, rms)
        s.level = _smooth(s.level, _norm(rms, self._peak))
        for i, b in enumerate(raw):
            self._band_peaks[i] = _decay_peak(self._band_peaks[i], b)
            s.bands[i] = _smooth(s.bands[i], _norm(b, self._band_peaks[i]))
        bass = sum(raw[:3]) / 3
        if self._bass_avg == 0:
            self._bass_avg = bass
        self._bass_avg += (bass - self._bass_avg) * 0.04
        beat = 0.0
        if self._bass_avg > 1e-6:
            beat = min(1.0, max(0.0, (bass / self._bass_avg - 1) / 0.5))
        s.beat += (beat - s.beat) * (0.75 if beat > s.beat else 0.28)
        return s


class AudioCapture:
    def __init__(self, source: str = ""):
        self.source = source
        self.error: Optional[Message] = None   # str() for display
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._analyzer = Analyzer()
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        self._cleanup()                  # a recorder that died must not linger
        cmd = recorder()
        if cmd is None:
            self.error = Message(N_("install pulseaudio-utils (parec) or pipewire (pw-record)"))
            return False
        source = self.source or default_monitor()
        if cmd[0] == "parec":
            cmd += ["-d", source] if source else []
            wav = False
        else:
            cmd += [f"--target={source}"] if source else []
            cmd += ["-"]
            wav = True
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.error = Message(N_("could not start {program}: {error}"), program=cmd[0], error=exc)
            return False
        self.error = None
        self._stop.clear()
        self._analyzer = Analyzer()
        self._thread = threading.Thread(target=self._read, args=(wav,), daemon=True,
                                        name="openalloy-audio")
        self._thread.start()
        log.info("Audio capture started (%s %s)", cmd[0], source or "default")
        return True

    def _read(self, wav: bool) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:      # explicit: asserts vanish under -O
            return
        try:
            if wav:
                proc.stdout.read(44)
            size = BLOCK * 2
            while not self._stop.is_set():
                raw = proc.stdout.read(size)
                if not raw or len(raw) < size:
                    break
                samples = [v / 32768.0 for v in struct.unpack(f"<{BLOCK}h", raw)]
                with self._lock:
                    self._analyzer.process(samples)
        except (OSError, ValueError) as exc:
            self.error = Message("{error}", error=exc)
        finally:
            with self._lock:
                self._analyzer.sound.on = False

    def frame(self) -> Sound:
        with self._lock:
            s = self._analyzer.sound
            return Sound(s.on and self.running, s.level, s.beat, list(s.bands))

    def _cleanup(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def stop(self) -> None:
        self._stop.set()
        self._cleanup()
