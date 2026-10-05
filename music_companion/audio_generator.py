"""Generate a short, deterministic WAV music loop from a recommended tempo.

The generator uses only Python's standard library. It creates a compact
four-beat loop with kick, hi-hat, bass, and a soft chord pad. This is not a
professional production tool; it is a local, dependency-free way to make the
MVP audible without relying on an external music API.
"""

from __future__ import annotations

import io
import math
import random
import wave
from array import array
from typing import Iterable


class AudioGenerationError(ValueError):
    """Raised when the requested audio parameters are invalid."""


SAMPLE_RATE = 22050
_MAX_AMPLITUDE = 32767

_BASS_PATTERN = (55.0, 55.0, 65.41, 49.0)
_PAD_NOTES = (220.0, 261.63, 329.63)
_ARP_NOTES = (440.0, 523.25, 659.25, 783.99)


def _kick(time_in_beat: float) -> float:
    if time_in_beat < 0 or time_in_beat >= 0.16:
        return 0.0
    frequency = 92.0 - 240.0 * time_in_beat
    return math.sin(2.0 * math.pi * frequency * time_in_beat) * math.exp(
        -18.0 * time_in_beat
    )


def _hat(time_in_beat: float, beat: float, rng: random.Random, gain: float = 0.18) -> float:
    hat_start = beat * 0.5
    local_time = time_in_beat - hat_start
    if local_time < 0 or local_time >= 0.045:
        return 0.0
    noise = rng.random() * 2.0 - 1.0
    return noise * math.exp(-70.0 * local_time) * gain


def _pluck(local_time: float, frequency: float) -> float:
    if local_time < 0 or local_time >= 0.34:
        return 0.0
    return (
        math.sin(2.0 * math.pi * frequency * local_time)
        * math.exp(-7.0 * local_time)
        * 0.24
    )


def _pad(time: float, frequency: float) -> float:
    return math.sin(2.0 * math.pi * frequency * time) * 0.055


def _arp(time: float, frequency: float) -> float:
    return math.sin(2.0 * math.pi * frequency * time) * 0.035


def _sine(time: float, frequency: float) -> float:
    return math.sin(2.0 * math.pi * frequency * time)


def _square(time: float, frequency: float) -> float:
    return 1.0 if math.sin(2.0 * math.pi * frequency * time) >= 0 else -1.0


def _saw(time: float, frequency: float) -> float:
    return 2.0 * ((time * frequency) % 1.0) - 1.0


def _triangle(time: float, frequency: float) -> float:
    phase = (time * frequency + 0.25) % 1.0
    return 2.0 * abs(2.0 * phase - 1.0) - 1.0


def _resolve_timbre(
    music_style: str,
    instruments: Iterable[str],
) -> dict[str, object]:
    instrument_text = ",".join(str(item) for item in instruments).lower()
    style_text = music_style.lower()

    if "合成器" in instrument_text or "electronic" in style_text or "电子" in style_text:
        return {
            "pad_wave": _saw,
            "arp_wave": _square,
            "pad_gain": 1.18,
            "arp_gain": 1.28,
            "hat_gain": 0.24,
            "bass_gain": 0.24,
        }
    if "钢琴" in instrument_text or "piano" in style_text or "治愈" in style_text:
        return {
            "pad_wave": _triangle,
            "arp_wave": _sine,
            "pad_gain": 0.86,
            "arp_gain": 0.78,
            "hat_gain": 0.08,
            "bass_gain": 0.18,
        }
    if "木吉他" in instrument_text or "acoustic" in style_text or "民谣" in style_text:
        return {
            "pad_wave": _sine,
            "arp_wave": _triangle,
            "pad_gain": 0.95,
            "arp_gain": 0.9,
            "hat_gain": 0.12,
            "bass_gain": 0.2,
        }
    return {
        "pad_wave": _sine,
        "arp_wave": _triangle,
        "pad_gain": 1.0,
        "arp_gain": 1.0,
        "hat_gain": 0.16,
        "bass_gain": 0.22,
    }


def _quantize_frequency(frequency: float, loop_duration: float) -> float:
    """Round a sustained tone so it completes an integer number of cycles per loop."""
    cycles = round(frequency * loop_duration)
    if cycles < 1:
        cycles = 1
    return cycles / loop_duration


def generate_music_wav(
    target_bpm: float,
    genres: Iterable[str] = (),
    music_style: str = "",
    instruments: Iterable[str] = (),
    duration_seconds: float = 1800.0,
) -> bytes:
    """Return a complete mono 16-bit WAV file as bytes.

    The default duration is 1800 seconds (30 minutes). To keep generation fast,
    a single seamless musical bar is synthesized and then repeated for the
    requested duration.
    """
    try:
        bpm = float(target_bpm)
        duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise AudioGenerationError("BPM 和时长必须是数字") from exc
    if not (40 <= bpm <= 220):
        raise AudioGenerationError("BPM 必须在 40 到 220 之间")
    if not (1 <= duration <= 3600):
        raise AudioGenerationError("音频时长必须在 1 到 3600 秒之间")

    genre_text = ",".join(str(genre) for genre in genres)
    style_text = str(music_style)
    instrument_text = ",".join(str(item) for item in instruments)
    rng = random.Random(f"{bpm:.2f}:{genre_text}:{style_text}:{instrument_text}")
    timbre = _resolve_timbre(style_text, instruments)
    pad_wave = timbre["pad_wave"]
    arp_wave = timbre["arp_wave"]
    pad_gain = float(timbre["pad_gain"])
    arp_gain = float(timbre["arp_gain"])
    hat_gain = float(timbre["hat_gain"])
    bass_gain = float(timbre["bass_gain"])
    # Use one four-beat bar as the repeating block. The block length is rounded
    # to a whole number of samples so the end of one copy joins the next copy
    # without a phase jump.
    loop_duration = (4.0 * 60.0) / bpm
    base_frames = max(1, int(round(loop_duration * SAMPLE_RATE)))
    loop_duration = base_frames / SAMPLE_RATE
    beat = loop_duration / 4.0
    pad_notes = tuple(
        _quantize_frequency(frequency, loop_duration) for frequency in _PAD_NOTES
    )
    arp_notes = tuple(
        _quantize_frequency(frequency, loop_duration) for frequency in _ARP_NOTES
    )
    total_frames = int(round(duration * SAMPLE_RATE))
    base_samples = array("h")

    for frame in range(base_frames):
        time = frame / SAMPLE_RATE
        beat_index = int(time // beat) % len(_BASS_PATTERN)
        time_in_beat = time % beat

        value = (
            _kick(time_in_beat)
            + _hat(time_in_beat, beat, rng, hat_gain)
            + _pluck(time_in_beat, _BASS_PATTERN[beat_index]) * bass_gain
            + pad_wave(time, pad_notes[0]) * 0.055 * pad_gain
            + pad_wave(time, pad_notes[1]) * 0.055 * pad_gain
            + arp_wave(time, arp_notes[beat_index]) * 0.035 * arp_gain
        )
        value = max(-1.0, min(1.0, value))
        base_samples.append(int(value * _MAX_AMPLITUDE))

    repeat_count, remainder = divmod(total_frames, base_frames)
    block = base_samples.tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for _ in range(repeat_count):
            wav.writeframes(block)
        if remainder:
            wav.writeframes(base_samples[:remainder].tobytes())
    return buffer.getvalue()
