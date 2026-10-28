"""Sleep frequencies for the Sleeping Sounds station, synthesized with numpy (no GPU, seconds to render).

Two jobs:
  * `layer_under(song, out, fx)`: lays a quiet binaural beat (left/right ear a few Hz apart: delta 0.5-4 Hz for deep
    sleep, theta 4-8 Hz for drifting off) plus an optional noise bed under a finished YuE2 render. Every song on a station
    whose `post_fx` has `binaural` goes through it.
  * `render_preset(key, seconds, out)`: a whole tone-only track (binaural carrier, 432 Hz-tuned pads, solfeggio tones,
    singing-bowl hits, brown/pink/rain/ocean noise). These are imported as ready songs so the station plays the moment it
    is tuned, before the first GPU render lands.
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import lfilter

import audio_utils

SR = 44100

BANDS = (("delta", 0.5, 4.0), ("theta", 4.0, 8.0), ("alpha", 8.0, 13.0))


def band_name(beat_hz: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= beat_hz < hi:
            return name
    return "delta" if beat_hz < 0.5 else "beta"


def db2lin(db: float) -> float:
    return float(10 ** (db / 20.0))


def a432(semitones_from_a4: float) -> float:
    """Pitch in the 432 Hz tuning people ask for ("432 Hz music")."""
    return 432.0 * 2 ** (semitones_from_a4 / 12.0)


def _t(n: int, sr: int = SR) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / sr


def sine(freq: float, n: int, sr: int = SR, phase: float = 0.0) -> np.ndarray:
    return np.sin(2 * math.pi * freq * _t(n, sr) + phase).astype(np.float32)


def slow_lfo(n: int, hz: float, depth: float, sr: int = SR, phase: float | None = None, rng: random.Random | None = None) -> np.ndarray:
    """1 +- depth, breathing slowly: keeps a sustained tone from feeling like a test signal."""
    rng = rng or random
    ph = rng.uniform(0, 2 * math.pi) if phase is None else phase
    return (1.0 + depth * np.sin(2 * math.pi * hz * _t(n, sr) + ph)).astype(np.float32)


def binaural(carrier_hz: float, beat_hz: float, n: int, sr: int = SR, rng: random.Random | None = None) -> np.ndarray:
    """(n, 2): left ear carrier - beat/2, right ear carrier + beat/2. The brain hears the difference as a slow pulse."""
    rng = rng or random
    left = sine(carrier_hz - beat_hz / 2, n, sr)
    right = sine(carrier_hz + beat_hz / 2, n, sr)
    breath = slow_lfo(n, rng.uniform(0.03, 0.07), 0.15, sr, rng=rng)
    return np.stack([left * breath, right * breath], axis=1)


def _normalize(x: np.ndarray, peak: float = 1.0) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return (x / m * peak).astype(np.float32) if m > 1e-9 else x.astype(np.float32)


def brown_noise(n: int, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    out = lfilter([1.0], [1.0, -0.995], white)  # one pole lowpass: the deep, soft "brown" roar
    return _normalize(out)


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
    a = [1.0, -2.494956002, 2.017265875, -0.522189400]
    return _normalize(lfilter(b, a, white))


def rain_noise(n: int, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    """Pink noise with the lows taken out and a slow gust to it: reads as steady rain on a roof."""
    pink = pink_noise(n, rng)
    rc = math.exp(-2 * math.pi * 700 / sr)
    hp = lfilter([1.0, -1.0], [1.0, -rc], pink)  # one pole highpass around 700 Hz
    py_rng = random.Random(int(rng.integers(1 << 30)))
    gust = slow_lfo(n, py_rng.uniform(0.05, 0.12), 0.25, sr, rng=py_rng) * slow_lfo(n, py_rng.uniform(0.3, 0.6), 0.12, sr, rng=py_rng)
    return _normalize(hp * gust)


def ocean_noise(n: int, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    """Brown noise under a wave envelope: each swell 9-14 s, never two the same."""
    brown = brown_noise(n, rng, sr)
    env = np.zeros(n, dtype=np.float32)
    i = 0
    while i < n:
        period = int(rng.uniform(9, 14) * sr)
        seg = min(period, n - i)
        shape = np.sin(np.linspace(0, math.pi, seg)) ** 1.6 * rng.uniform(0.55, 1.0) + 0.12
        env[i:i + seg] = shape[:seg]
        i += seg
    return _normalize(brown * env)


NOISES = {"brown": brown_noise, "pink": pink_noise, "rain": rain_noise, "ocean": ocean_noise}


def pad(freqs: list[float], n: int, sr: int = SR, rng: random.Random | None = None, detune_hz: float = 0.35) -> np.ndarray:
    """(n, 2) warm additive pad: each note = fundamental + soft 2nd/3rd partials, detuned twice for width, breathing slowly."""
    rng = rng or random
    out = np.zeros((n, 2), dtype=np.float32)
    for f in freqs:
        for side, sign in ((0, -1), (1, 1)):
            for mult, gain in ((1.0, 1.0), (2.0, 0.28), (3.0, 0.12)):
                out[:, side] += sine(f * mult + sign * detune_hz * rng.uniform(0.5, 1.5), n, sr, rng.uniform(0, 6.28)) * gain
        out *= 1.0  # keep dtype
        out[:, 0] *= slow_lfo(n, rng.uniform(0.02, 0.06), 0.2, sr, rng=rng)
        out[:, 1] *= slow_lfo(n, rng.uniform(0.02, 0.06), 0.2, sr, rng=rng)
    return _normalize(out)


def bowls(base_hz: float, n: int, sr: int = SR, rng: random.Random | None = None, every_s: tuple[float, float] = (14.0, 26.0)) -> np.ndarray:
    """(n, 2) singing-bowl strikes: inharmonic partials (1, 2.71, 5.42, 8.9) with long decays and the slow wobble a real
    bowl has, struck every 14-26 s at a random strength, panned a little left or right."""
    rng = rng or random
    out = np.zeros((n, 2), dtype=np.float32)
    partials = ((1.0, 1.0, 9.0), (2.71, 0.45, 5.0), (5.42, 0.2, 3.0), (8.9, 0.08, 1.6))
    t_hit = rng.uniform(2.0, 6.0)
    while t_hit * sr < n:
        start = int(t_hit * sr)
        length = min(int(14 * sr), n - start)
        tt = _t(length, sr)
        hit = np.zeros(length, dtype=np.float32)
        for ratio, gain, tau in partials:
            f = base_hz * ratio
            wob = rng.uniform(0.8, 2.2)
            hit += (np.sin(2 * math.pi * f * tt) + np.sin(2 * math.pi * (f + wob) * tt)) * 0.5 * gain * np.exp(-tt / tau)
        hit *= np.minimum(1.0, tt / 0.012)  # 12 ms attack
        amp = rng.uniform(0.45, 1.0)
        pan = rng.uniform(0.35, 0.65)
        out[start:start + length, 0] += hit * amp * (1 - pan) * 1.4
        out[start:start + length, 1] += hit * amp * pan * 1.4
        t_hit += rng.uniform(*every_s)
    return _normalize(out)


def fade(x: np.ndarray, seconds: float, sr: int = SR) -> np.ndarray:
    k = min(int(seconds * sr), len(x) // 2)
    if k <= 0:
        return x
    ramp = np.linspace(0, 1, k, dtype=np.float32)
    shape = (k, 1) if x.ndim == 2 else (k,)
    x[:k] *= ramp.reshape(shape)
    x[-k:] *= ramp[::-1].reshape(shape)
    return x


def _stereo(y: np.ndarray) -> np.ndarray:
    if y.ndim == 1:
        return np.stack([y, y], axis=1)
    if y.shape[1] == 1:
        return np.repeat(y, 2, axis=1)
    return y[:, :2]


# ----------------------------------------------------------------------------------------------- under a rendered song
def layer_under(song_path: str | Path, out_path: str | Path, fx: dict[str, Any], rng: random.Random | None = None) -> dict[str, Any]:
    """Mix a binaural beat (and a noise bed if asked) under a finished song. Levels are dBFS of the bed's own peak; the
    song is left alone and only soft-limited afterwards. Returns what was laid down so the song can carry it in its tags."""
    rng = rng or random.Random()
    y, sr = audio_utils.load_audio(song_path)
    y = _stereo(np.asarray(y, dtype=np.float32))
    n = len(y)
    c_lo, c_hi = fx.get("carrier_hz") or (150.0, 220.0)
    b_lo, b_hi = fx.get("beat_hz") or (1.5, 6.0)
    carrier = rng.uniform(float(c_lo), float(c_hi))
    beat = rng.uniform(float(b_lo), float(b_hi))
    bed = binaural(carrier, beat, n, sr, rng) * db2lin(float(fx.get("level_db", -21)))
    noise = fx.get("noise")
    if noise in NOISES:
        nrng = np.random.default_rng(rng.getrandbits(32))
        bed += _stereo(NOISES[noise](n, nrng, sr) if noise != "pink" else NOISES[noise](n, nrng)) * db2lin(float(fx.get("noise_db", -36)))
    bed = fade(bed, float(fx.get("fade_s", 6.0)), sr)
    out = audio_utils.soft_limit(y + bed)
    sf.write(str(out_path), out.astype(np.float32), sr)
    info = {"carrier_hz": round(carrier), "beat_hz": round(beat, 1), "band": band_name(beat), "level_db": float(fx.get("level_db", -21)), "noise": noise or None}
    return info


# ----------------------------------------------------------------------------------------------- whole tone tracks
PRESETS: dict[str, dict[str, Any]] = {
    "delta-drift": {"title": "Delta Drift", "blurb": "2 Hz delta beat on a 180 Hz carrier, brown noise, a slow F major pad in 432 tuning",
                    "carrier": 180.0, "beat": 2.0, "tone_db": -14, "noise": "brown", "noise_db": -26,
                    "pad": [a432(-16), a432(-9), a432(-4), a432(-2)], "pad_db": -19},
    "theta-rain": {"title": "Theta Rain", "blurb": "6 Hz theta beat on 210 Hz under steady rain, a thin high pad",
                   "carrier": 210.0, "beat": 6.0, "tone_db": -16, "noise": "rain", "noise_db": -17,
                   "pad": [a432(-5), a432(2), a432(7)], "pad_db": -26},
    "432-drone": {"title": "432 Hz Drone", "blurb": "A drone on A (432 Hz and its octaves) with a 3 Hz delta beat inside it",
                  "carrier": a432(-12), "beat": 3.0, "tone_db": -12, "noise": None,
                  "pad": [a432(-24), a432(-12), a432(-5), a432(0)], "pad_db": -15},
    "528-bowls": {"title": "528 Hz Bowls", "blurb": "Singing bowls tuned to 528 Hz over a 1.5 Hz delta beat and a faint brown-noise floor",
                  "carrier": 132.0, "beat": 1.5, "tone_db": -14, "noise": "brown", "noise_db": -34,
                  "bowls": 528.0, "bowls_db": -14, "pad": [132.0, 264.0], "pad_db": -24},
    "ocean-delta": {"title": "Ocean Delta", "blurb": "Waves rolling in every 9-14 s, a 3 Hz delta beat on 150 Hz, a low warm pad",
                    "carrier": 150.0, "beat": 3.0, "tone_db": -15, "noise": "ocean", "noise_db": -14,
                    "pad": [a432(-21), a432(-14), a432(-9)], "pad_db": -21},
    "solfeggio-pad": {"title": "Solfeggio Pad", "blurb": "396, 417, 528 and 639 Hz tones breathing over each other, 4.5 Hz theta beat on 198 Hz",
                      "carrier": 198.0, "beat": 4.5, "tone_db": -15, "noise": "pink", "noise_db": -36,
                      "pad": [396.0, 417.0, 528.0, 639.0], "pad_db": -17},
}


def render_preset(key: str, seconds: float, out_path: str | Path, sr: int = SR, seed: int | None = None) -> dict[str, Any]:
    p = PRESETS[key]
    rng = random.Random(seed)
    nrng = np.random.default_rng(rng.getrandbits(32))
    n = int(seconds * sr)
    out = binaural(p["carrier"], p["beat"], n, sr, rng) * db2lin(p["tone_db"])
    if p.get("noise") in NOISES:
        fn = NOISES[p["noise"]]
        nz = fn(n, nrng) if p["noise"] == "pink" else fn(n, nrng, sr)
        out += _stereo(nz) * db2lin(p["noise_db"])
    if p.get("pad"):
        out += pad(p["pad"], n, sr, rng) * db2lin(p["pad_db"])
    if p.get("bowls"):
        out += bowls(p["bowls"], n, sr, rng) * db2lin(p["bowls_db"])
    out = fade(out, 8.0, sr)
    out = audio_utils.soft_limit(out, ceiling=0.7)  # sleep music: leave headroom, never loud
    sf.write(str(out_path), out.astype(np.float32), sr)
    return {"key": key, "title": p["title"], "blurb": p["blurb"], "carrier_hz": round(p["carrier"]), "beat_hz": p["beat"], "band": band_name(p["beat"]), "seconds": seconds}


if __name__ == "__main__":  # python sleeptones.py <out_dir> [seconds]
    import sys
    out_dir = Path(sys.argv[1])
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    out_dir.mkdir(parents=True, exist_ok=True)
    for k in PRESETS:
        info = render_preset(k, secs, out_dir / f"{k}.wav")
        print(info)
