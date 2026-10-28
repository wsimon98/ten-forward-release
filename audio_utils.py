"""audio_utils.py — mixing, normalizing, converting and trimming helpers (ffmpeg + numpy)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

TF_ROOT = Path(__file__).resolve().parent
_FFMPEG_CANDIDATES = [
    Path(os.environ.get("TF_WANGP_ROOT") or (Path(__file__).resolve().parent / "wan2gp")) / "ffmpeg_bins" / "ffmpeg.exe",
    Path(__file__).resolve().parent / "wan2gp" / "ffmpeg_bins" / "ffmpeg.exe",
]


def ffmpeg_bin() -> str:
    for c in _FFMPEG_CANDIDATES:
        if c.exists():
            return str(c)
    found = shutil.which("ffmpeg")
    return found or "ffmpeg"


def ffprobe_bin() -> str:
    ff = Path(ffmpeg_bin())
    probe = ff.with_name("ffprobe.exe") if ff.suffix else ff.with_name("ffprobe")
    if probe.exists():
        return str(probe)
    return shutil.which("ffprobe") or "ffprobe"


def duration_seconds(path: str | os.PathLike) -> float:
    try:
        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        pass
    try:
        out = subprocess.run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def run_ffmpeg(args: list[str], timeout: int = 600) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[:800]}")


def to_mp3(src: str | os.PathLike, dst: str | os.PathLike, bitrate: str = "192k", fade_out: float = 0.0) -> str:
    """Encode to mp3; fade_out > 0 fades the last N seconds (used when YuE2 hit its token cap and the ending was clipped)."""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(src), "-vn"]
    if fade_out and fade_out > 0:
        total = duration_seconds(src)
        if total > fade_out + 1:
            args += ["-af", f"afade=t=out:st={total - fade_out:.2f}:d={fade_out:.2f}"]
    args += ["-codec:a", "libmp3lame", "-b:a", bitrate, "-ar", "48000", str(dst)]
    run_ffmpeg(args)
    return str(dst)


def to_wav(src: str | os.PathLike, dst: str | os.PathLike, sr: int | None = None, mono: bool = False) -> str:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(src), "-vn"]
    if sr:
        args += ["-ar", str(sr)]
    if mono:
        args += ["-ac", "1"]
    args += ["-c:a", "pcm_s16le", str(dst)]
    run_ffmpeg(args)
    return str(dst)


def load_audio(path: str | os.PathLike, sr: int | None = None, mono: bool = False) -> tuple[np.ndarray, int]:
    """Returns float32 array [samples, channels] and sample rate (resampled with ffmpeg if needed)."""
    p = Path(path)
    data, rate = sf.read(str(p), dtype="float32", always_2d=True)
    if (sr and rate != sr) or (mono and data.shape[1] > 1 and False):
        tmp = p.with_suffix(f".tmp{sr}.wav")
        to_wav(p, tmp, sr=sr)
        data, rate = sf.read(str(tmp), dtype="float32", always_2d=True)
        try:
            tmp.unlink()
        except OSError:
            pass
    if mono and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    return data, rate


def _match_channels(a: np.ndarray, channels: int) -> np.ndarray:
    if a.shape[1] == channels:
        return a
    if a.shape[1] == 1:
        return np.repeat(a, channels, axis=1)
    return a.mean(axis=1, keepdims=True).repeat(channels, axis=1)


def loudness_normalize(data: np.ndarray, rate: int, target_lufs: float = -14.0) -> np.ndarray:
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(rate)
        loud = meter.integrated_loudness(data)
        if np.isfinite(loud):
            gain_db = target_lufs - loud
            gain_db = max(min(gain_db, 20.0), -20.0)
            data = data * (10 ** (gain_db / 20))
    except Exception:
        peak = float(np.max(np.abs(data)) or 1.0)
        data = data * (0.89 / peak)
    return soft_limit(data)


def soft_limit(data: np.ndarray, ceiling: float = 0.98) -> np.ndarray:
    peak = float(np.max(np.abs(data)) or 0.0)
    if peak <= ceiling:
        return data
    # gentle tanh knee only on the part above the ceiling
    x = data / peak
    return np.tanh(x * 1.2) / np.tanh(1.2) * ceiling


def mix_vocals_instrumental(vocals: str | os.PathLike, instrumental: str | os.PathLike, out_path: str | os.PathLike, vocal_gain_db: float = 0.0, target_lufs: float = -14.0, sr: int = 48000) -> str:
    v, _ = load_audio(vocals, sr=sr)
    i, _ = load_audio(instrumental, sr=sr)
    channels = max(v.shape[1], i.shape[1], 2)
    v = _match_channels(v, channels)
    i = _match_channels(i, channels)
    n = max(len(v), len(i))
    if len(v) < n:
        v = np.pad(v, ((0, n - len(v)), (0, 0)))
    if len(i) < n:
        i = np.pad(i, ((0, n - len(i)), (0, 0)))
    mix = i + v * (10 ** (vocal_gain_db / 20))
    mix = loudness_normalize(mix, sr, target_lufs)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), mix.astype(np.float32), sr, subtype="PCM_24")
    return str(out_path)


def normalize_file(src: str | os.PathLike, dst: str | os.PathLike, target_lufs: float = -14.0) -> str:
    data, rate = load_audio(src)
    data = loudness_normalize(data, rate, target_lufs)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), data.astype(np.float32), rate, subtype="PCM_24")
    return str(dst)


def trim_voice_sample(src: str | os.PathLike, dst: str | os.PathLike, max_seconds: float = 30.0, sr: int = 44100) -> dict:
    """Mono 44.1 kHz voice sample: keeps the loudest `max_seconds` window (skips silence), peak -3 dBFS."""
    data, rate = load_audio(src, sr=sr, mono=True)
    x = data[:, 0]
    total = len(x) / rate
    if total > max_seconds:
        frame = rate // 2  # 0.5 s frames
        n_frames = len(x) // frame
        energy = np.array([float(np.sqrt(np.mean(x[k * frame:(k + 1) * frame] ** 2))) for k in range(n_frames)])
        win = int(max_seconds / 0.5)
        if win < n_frames:
            sums = np.convolve(energy, np.ones(win), mode="valid")
            start = int(np.argmax(sums)) * frame
            x = x[start:start + int(max_seconds * rate)]
    # trim leading/trailing silence
    thresh = 0.01 * (np.max(np.abs(x)) or 1.0)
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) > 0:
        x = x[max(0, idx[0] - rate // 4): min(len(x), idx[-1] + rate // 4)]
    peak = float(np.max(np.abs(x)) or 1.0)
    x = x * (0.7079 / peak)  # -3 dBFS
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), x.astype(np.float32), rate, subtype="PCM_16")
    return {"path": str(dst), "seconds": round(len(x) / rate, 2), "source_seconds": round(total, 2)}
