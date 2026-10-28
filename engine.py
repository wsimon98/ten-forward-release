"""
engine.py — Ten Forward's bridge into the Wan2GP v13 install (YuE2, SheetSage2, BS-RoFormer, Seed-VC).

Runs in Ten Forward's own runtime (CPython 3.11 with torch + CUDA)
against Ten Forward's own copy of Wan2GP v13 in wan2gp/ (code + ckpts), or TF_WANGP_ROOT if that is set
Uses Wan2GP's in-process API (shared/api.py).

GPU etiquette on a shared card:
  * one GPU job at a time inside this process (self._lock)
  * gpu-butler lease around every GPU job (fail-open if the butler is down)
  * before loading, ask ComfyUI to drop cached models when its queue is empty
  * unload() releases YuE2 + Seed-VC so other apps' renders get the card back
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import types
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

TF_ROOT = Path(__file__).resolve().parent
WANGP_ROOT = Path(os.environ.get("TF_WANGP_ROOT") or (TF_ROOT / "wan2gp"))  # our own Wan2GP tree, next to this file
CONFIG_PATH = Path(os.environ.get("TF_WANGP_CONFIG") or (TF_ROOT / "wangp" / "wgp_config.json"))
RAW_DIR = TF_ROOT / "library" / "_raw"
COMFY_URL = os.environ.get("TF_COMFY_URL", "http://127.0.0.1:8188")
MODEL_TYPE = os.environ.get("TF_MODEL_TYPE", "yue2")
WHISPER_MODEL = os.environ.get("TF_WHISPER_MODEL", "small")
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".opus"}

LOG = logging.getLogger("tenforward.engine")

# ---- which Wan2GP is this? Feature-detect from the tree itself, so ONE engine.py runs on v13.02 and on v18.x.
_YUE = WANGP_ROOT / "models" / "TTS" / "yue2"


def _wangp_version() -> str:
    try:
        import re as _re
        m = _re.search(r'^WanGP_version\s*=\s*"([^"]+)"', (WANGP_ROOT / "wgp.py").read_text(encoding="utf-8", errors="replace"), _re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


WANGP_VERSION = _wangp_version()
NATIVE_LM = (WANGP_ROOT / "shared" / "llm_engines" / "lmgp").is_dir()   # v18.21+: one LM engine (CUDA graphs + Triton + FA2), no "legacy" switch, AR LoRAs are native
HAS_COMPOSITION = (_YUE / "composition.py").is_file()                   # v13.141+: audio_prompt_type 12 / A2 = continue an ABC score / a score transcribed from audio
HAS_INSTRUMENTAL = (_YUE / "instrumental.py").is_file()                 # v13.1315+: model_mode 3, built-in instrumental planner LoRA
HAS_HUM = (_YUE / "hum.py").is_file()                                   # v13.1315+: model_type yue2_hum (Hum-to-Song)
HUM_MODEL_TYPE = "yue2_hum"
if NATIVE_LM and not os.environ.get("TF_WANGP_CONFIG"):
    # v18 migrates the config file in place (new keys, retired ones dropped, e.g. lm_decoder_engine): keep that off the file v13 reads,
    # so switching back to the old tree is safe. First run copies the v13 file.
    _V18_CONFIG = TF_ROOT / "wangp-v18" / "wgp_config.json"    # Wan2GP insists the file is called wgp_config.json, hence a folder of its own
    if not _V18_CONFIG.exists() and CONFIG_PATH.exists():
        _V18_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONFIG_PATH, _V18_CONFIG)
    CONFIG_PATH = _V18_CONFIG
def music_model_type() -> str:
    """The Wan2GP model type the next song is made with: the Settings 'Music model' (stock, or a finetune entry
    finetunes/yue2_<name>.json written by tools/convert_base.py). A name whose entry is gone falls back to stock."""
    try:
        import config as _config
        name = str(_config.cfg("music_model") or "stock").strip().lower()
    except Exception:
        return MODEL_TYPE
    if name in ("", "stock", MODEL_TYPE):
        return MODEL_TYPE
    if (WANGP_ROOT / "finetunes" / f"yue2_{name}.json").is_file():
        return f"yue2_{name}"
    LOG.warning("music model %r has no finetunes/yue2_%s.json in %s: using stock", name, name, WANGP_ROOT)
    return MODEL_TYPE


INST_LORA = "ar_lora_inst_v3abc.bf16.safetensors"                    # Ten Forward's own copy of the planner LoRA (loras/): replaced by mode 3 where the tree has it

# Wan2GP uses relative paths (ckpts/, loras/) so the process must live in its root.
if str(WANGP_ROOT) not in sys.path:
    sys.path.insert(0, str(WANGP_ROOT))
os.chdir(WANGP_ROOT)
if str(TF_ROOT) not in sys.path:
    sys.path.insert(0, str(TF_ROOT))

sys.path.insert(0, r"C:\apps\gpu-butler")
try:
    from gpu_lease import gpu_lease  # type: ignore
except Exception:  # butler client missing: fail open
    @contextmanager
    def gpu_lease(*_a, **_k):
        yield


class Cancelled(Exception):
    pass


def _job_done(job) -> bool:
    """SessionJob.done is a property in some Wan2GP builds and a method in others."""
    d = getattr(job, "done", None)
    try:
        return bool(d()) if callable(d) else bool(d)
    except Exception:
        return False


class _Callbacks:
    """Adapts Wan2GP callback methods to a single on_progress(**fields) function."""

    def __init__(self, fn: Callable[..., None] | None):
        self.fn = fn
        self.lines: list[str] = []

    def _emit(self, **fields):
        if self.fn is None:
            return
        try:
            self.fn(**fields)
        except Exception:
            LOG.exception("progress callback failed")

    def on_progress(self, p):
        self._emit(phase=getattr(p, "phase", "") or "", status=getattr(p, "status", "") or "", progress=getattr(p, "progress", None), step=getattr(p, "current_step", None), total=getattr(p, "total_steps", None))

    def on_status(self, text):
        self._emit(status=str(text))

    def on_info(self, text):
        self._emit(info=str(text))

    def on_stream(self, line):
        text = getattr(line, "text", "")
        if text:
            self.lines.append(text)
            if len(self.lines) > 400:
                del self.lines[:200]

    def on_error(self, error):
        msg = str(getattr(error, "message", error))
        # A cancel is how a background song steps aside for something you asked for, and how the
        # Cancel button works. Both are things we did on purpose, so they do not belong at ERROR -
        # they would bury the failures worth looking at.
        if "cancel" in msg.lower():
            LOG.info("wan2gp: %s", msg)
        else:
            LOG.error("wan2gp error: %s", msg)


class Engine:
    def __init__(self, output_dir: str | os.PathLike = RAW_DIR):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._session = None
        self._lock = threading.RLock()
        self._whisper = None
        self._pending_loras: dict[str, float] | None = None
        self.last_lora_report: dict[str, Any] | None = None
        self.last_plan: dict[str, Any] | None = None
        self.last_truncated: dict[str, Any] = {}  # Wan2GP's per-phase "hit max_tokens" flags for the last render
        self.busy = False
        self.loaded_at: float | None = None
        self.last_used = time.time()
        self.current_job = None
        self.engine_mode = "native" if NATIVE_LM else ""  # "" = Wan2GP default LM engine (CUDA graphs when possible), "legacy" for AR LoRAs (v13); "native" = v18, no choice

    # ------------------------------------------------------------------ session
    def session(self):
        with self._lock:
            if self._session is None:
                from shared.api import init  # type: ignore
                LOG.info("initializing Wan2GP session root=%s config=%s", WANGP_ROOT, CONFIG_PATH)
                self._session = init(root=WANGP_ROOT, config_path=CONFIG_PATH, output_dir=self.output_dir, console_output=True, console_isatty=False)
                self._patch_pipeline()
                self.loaded_at = time.time()
            return self._session

    def _patch_pipeline(self):
        """Wrap YuE2Pipeline.generate so LoRA hooks are applied per job and the score plan is captured."""
        from models.TTS.yue2.pipeline import YuE2Pipeline  # type: ignore
        if getattr(YuE2Pipeline, "_tf_patched", False):
            return
        original = YuE2Pipeline.generate
        engine = self

        def generate(pipe, *args, **kwargs):
            plan = engine._pending_loras or {}
            sess = None
            if plan:
                from loras import LoraSession
                sess = LoraSession()
                # v18: the AR LM runs in CUDA graphs, where Python forward hooks are bypassed, and Wan2GP loads AR LoRAs itself (see
                # generate_song), so our hooks only ever go on the acoustic (decoder) side there
                target = types.SimpleNamespace(text_encoder=None, transformer=pipe.transformer) if NATIVE_LM else pipe
                report = sess.apply(target, plan)
                engine.last_lora_report = report
                LOG.info("LoRA hooks applied: %s", json.dumps(report))
            try:
                return original(pipe, *args, **kwargs)
            finally:
                if sess is not None:
                    engine.last_lora_report = dict(engine.last_lora_report or {}, calls=sess.calls())
                    sess.remove()
                engine.last_plan = getattr(pipe, "last_plan", None)
                engine.last_truncated = dict(getattr(pipe, "last_truncated", None) or {})

        YuE2Pipeline.generate = generate
        YuE2Pipeline._tf_patched = True

    def is_loaded(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------------ GPU etiquette
    # Below this much free VRAM it is worth asking ComfyUI to let go before a render. Above it,
    # the two HTTP calls (and ComfyUI reloading its models afterwards for nothing) are pure cost:
    # they were paid before EVERY song regardless of whether the card was under any pressure.
    FREE_COMFY_BELOW_MB = 12000

    def comfy_free_if_idle(self, need_mb: int | None = None) -> bool:
        try:
            free_mb = int((self.vram() or {}).get("free_mb") or 0)
            if free_mb >= (need_mb or self.FREE_COMFY_BELOW_MB):
                return False                      # there is room; leave ComfyUI alone
            with urllib.request.urlopen(COMFY_URL + "/queue", timeout=3) as r:
                q = json.loads(r.read() or b"{}")
            if q.get("queue_running") or q.get("queue_pending"):
                return False
            req = urllib.request.Request(COMFY_URL + "/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20):
                pass
            return True
        except Exception:
            return False

    def comfy_busy(self) -> bool:
        try:
            with urllib.request.urlopen(COMFY_URL + "/queue", timeout=3) as r:
                q = json.loads(r.read() or b"{}")
            return bool(q.get("queue_running") or q.get("queue_pending"))
        except Exception:
            return False

    def _set_engine_mode(self, mode: str):
        """Switch the LM decoder engine ("" default or "legacy"); forces a model reload on change."""
        if NATIVE_LM:
            return    # v18 has a single LM engine that supports AR LoRAs; nothing to switch
        import wgp  # type: ignore
        current = wgp.server_config.get("lm_decoder_engine", "")
        if current == mode:
            return
        LOG.info("switching LM engine %r -> %r (model will reload)", current, mode)
        wgp.server_config["lm_decoder_engine"] = mode
        try:
            wgp.lm_decoder_engine = mode
        except Exception:
            pass
        try:
            wgp.release_model()
        except Exception:
            LOG.exception("release_model during engine switch")
        self.engine_mode = mode

    def unload(self):
        """Release YuE2 and Seed-VC from VRAM (keeps the Python session alive)."""
        with self._lock:
            if self._session is None:
                return
            try:
                import wgp  # type: ignore
                wgp.release_model()
            except Exception:
                LOG.exception("release_model failed")
            try:
                try:
                    from postprocessing.seedvc_processor import bridge as wgp_bridge  # type: ignore   # v18
                except ImportError:
                    from postprocessing.seedvc import wgp_bridge  # type: ignore                        # v13
                wgp_bridge.release_models()
            except Exception:
                pass
            self._free_whisper()
            try:
                import voice_ft  # type: ignore
                voice_ft.release()
            except Exception:
                pass
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            LOG.info("models released")

    def vram(self) -> dict[str, Any]:
        if NATIVE_LM and self._session is None:
            # v18 must install its VRAM allocator BEFORE this process touches CUDA, and /api/status asks for the VRAM long before the
            # first job: read it from the driver (NVML) instead of torch.cuda, which would initialise CUDA and cost every LM its
            # resident blocks (measured: 100 -> 6 tokens/s).
            try:
                import pynvml  # type: ignore
                pynvml.nvmlInit()
                info = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0))
                return {"free_mb": int(info.free / 2**20), "total_mb": int(info.total / 2**20)}
            except Exception:
                return {}
        try:
            import torch
            free, total = torch.cuda.mem_get_info()
            return {"free_mb": int(free / 2**20), "total_mb": int(total / 2**20)}
        except Exception:
            return {}

    # ------------------------------------------------------------------ generation
    def defaults(self) -> dict[str, Any]:
        return self.session().get_default_settings(MODEL_TYPE)

    def generate_song(self, *, lyrics: str, style: str, mode: int = 0, duration: int = 120, steps: int = 32, seed: int = -1, cfg: float = 1.0, temperature: float = 1.0, top_k: int = 100, top_p: float = 0.95, abc_path: str | None = None, source_audio: str | None = None, save_score: bool = True, loras: dict[str, float] | None = None, on_progress: Callable[..., None] | None = None, cancel: threading.Event | None = None,
                      extend: bool = False, instrumental: bool = False, hum: bool = False) -> dict[str, Any]:
        """Render one song. Returns {audio, abc, mid, plan, lora_report, seed}.
        New with Wan2GP v13.141+/v18 (each raises a clear error on a tree that lacks it):
          extend=True      with abc_path: continue that ABC score (audio_prompt_type 12); with source_audio: transcribe the recording
                           (SheetSage2), continue its score, render the whole (A2). The song is a NEW take: lyrics + style are re-sung.
          instrumental=True  model_mode 3 with the built-in planner LoRA (lyrics = [Instrumental] or section tags, see instrumental.py).
                           The old AR LoRA request {INST_LORA: 1.0} is turned into this automatically.
          hum=True         Hum-to-Song (model yue2_hum): source_audio is the hum; mode 0 = Continue Hum, 1 = Hum Only."""
        plan = {k: float(v) for k, v in (loras or {}).items() if v}
        model_type = music_model_type()
        native_loras: dict[str, float] = {}
        audio_prompt_type = ""
        inst_from_lora = False
        if hum:
            if not HAS_HUM:
                raise RuntimeError(f"Hum-to-Song needs a newer Wan2GP (this is {WANGP_VERSION})")
            if not source_audio:
                raise ValueError("Hum-to-Song needs the hummed recording as source_audio")
            model_type, audio_prompt_type, mode, abc_path, plan = HUM_MODEL_TYPE, "A", (0 if int(mode) == 0 else 1), None, {}   # the hum adapter + real-audio LoRA are part of the model; no manual LoRAs, no ABC
        else:
            inst_from_lora = INST_LORA in plan and HAS_INSTRUMENTAL and not source_audio and not instrumental
            if inst_from_lora:
                instrumental = True
            if instrumental:
                if not HAS_INSTRUMENTAL:
                    raise RuntimeError(f"Instrumental mode needs a newer Wan2GP (this is {WANGP_VERSION})")
                plan.pop(INST_LORA, None)    # mode 3 loads the planner LoRA by itself
                mode = 3
                if not re.match(r"\s*instrumental\b", style or "", re.I):
                    style = "instrumental, " + (style or "")   # the instrumental planner was taught styles that start so
                if not re.fullmatch(r"\s*(\[(intro|verse|pre-chorus|chorus|bridge|outro|instrumental)[^\]]*\]\s*)+", lyrics or "", re.I):
                    lyrics = "[Instrumental]"                  # words or other tags would fail its plan check
            if source_audio:
                audio_prompt_type = "A2" if extend else "A"
            elif abc_path:
                audio_prompt_type = ("12" if extend else "1") if HAS_COMPOSITION else ""
            if extend and not HAS_COMPOSITION:
                raise RuntimeError(f"Extend-a-song needs a newer Wan2GP (this is {WANGP_VERSION})")
            if extend and not (source_audio or abc_path):
                raise ValueError("extend=True needs abc_path or source_audio")
            if (abc_path or source_audio) and int(mode) == 2:
                mode = 0
        if plan:
            # a LoRA trained with a trigger word only wakes up when the word is in the tags (GRVL 'grvl', death metal
            # 'deathmetal', the style packs' own words): put each one in front of the style, once
            try:
                from loras import with_triggers as _with_triggers
                style, _added = _with_triggers(style, plan)
                if _added:
                    LOG.info("LoRA trigger words added to the style: %s", ", ".join(_added))
            except Exception:
                LOG.warning("could not add LoRA trigger words", exc_info=True)
        if NATIVE_LM and plan:
            # v18 loads AR (planner) LoRAs itself, on the fast LM engine: hand those over by absolute path. Decoder (nar) LoRAs stay
            # on our forward hooks: Wan2GP's loader refuses files with full replacement weights (vae2llm/llm2vae), which Mothersuperior's
            # nar_lora_joint_v4 has.
            from loras import scan as _scan_loras
            kinds = {l["file"]: l["kind"] for l in _scan_loras()}
            hooks: dict[str, float] = {}
            for fname, scale in plan.items():
                path = TF_ROOT / "loras" / fname
                if fname.startswith("hum_adapter"):
                    LOG.warning("skipping %s: Hum-to-Song is built in (model yue2_hum)", fname)
                elif not path.exists():
                    LOG.warning("LoRA %s not found in %s", fname, path.parent)
                elif kinds.get(fname) == "ar":
                    native_loras[str(path)] = float(scale)
                else:
                    if kinds.get(fname) == "both":
                        # the planner half goes to Wan2GP's own loader (lora_convert.write_plan_half); the sound half stays on our hooks
                        plan_half = TF_ROOT / "loras" / "_plan" / (Path(fname).stem + ".plan.safetensors")
                        if plan_half.exists():
                            native_loras[str(plan_half)] = float(scale)
                        else:
                            LOG.warning("%s touches the AR planner too: only its decoder half is applied (no _plan file)", fname)
                    hooks[fname] = float(scale)
            plan = hooks
        overrides = {
            "model_type": model_type,
            "prompt": lyrics,
            "alt_prompt": style,
            "model_mode": int(mode),
            "duration_seconds": int(duration),
            "video_length": 0,
            "num_inference_steps": int(steps),
            "seed": int(seed),
            "guidance_scale": float(cfg),
            "temperature": float(temperature),
            "top_k": max(0, min(100, int(top_k))),   # Wan2GP caps Top-k at 100 (shared/extra_settings.py); above it the job dies in a second
            "top_p": float(top_p),
            "custom_settings": {"save_score": 1 if save_score else 0},
            "audio_prompt_type": audio_prompt_type if (HAS_COMPOSITION or hum) else ("A" if source_audio else ""),
            "audio_guide": str(source_audio) if source_audio else None,
            "custom_guide": str(abc_path) if (abc_path and not source_audio) else None,
            "repeat_generation": 1,
            "prompt_enhancer": "",
            "negative_prompt": "",
        }
        # always said, even when empty: left out, Wan2GP takes the model's default settings' list, and the report of
        # the song before would be shown for this one (mode 3's own instrumental LoRA is added by Wan2GP either way)
        overrides["activated_loras"] = list(native_loras)
        overrides["loras_multipliers"] = " ".join(f"{v:g}" for v in native_loras.values())
        self.last_lora_report = None
        callbacks = _Callbacks(on_progress)
        with self._lock:
            self.busy = True
            self.last_used = time.time()
            try:
                session = self.session()
                if int(mode) == 3 and HAS_INSTRUMENTAL:
                    # checked here, not earlier: importing Wan2GP's modules before session() would initialise CUDA ahead of its VRAM allocator
                    from models.TTS.yue2.instrumental import validate_instrumental_prompt  # type: ignore
                    bad = validate_instrumental_prompt(lyrics)
                    if bad and inst_from_lora and (TF_ROOT / "loras" / INST_LORA).exists():
                        # the caller only asked for the planner LoRA, with lyrics mode 3 does not accept ([Solo], [Final Chorus]...):
                        # keep the classic planner and load the same LoRA the way it always was, natively on v18 (slower than mode 3)
                        LOG.warning("instrumental plan not valid for mode 3 (%s): classic planning with the LoRA", bad)
                        overrides["model_mode"] = 0
                        overrides["activated_loras"] = [str(TF_ROOT / "loras" / INST_LORA)]
                        overrides["loras_multipliers"] = "1"
                    elif bad:
                        raise ValueError("instrumental plan rejected by Wan2GP: " + str(bad))
                settings = session.get_default_settings(model_type)
                settings.update(overrides)
                with gpu_lease("tenforward"):
                    self.comfy_free_if_idle()
                    if plan:
                        from loras import uses_ar
                        self._set_engine_mode("legacy" if uses_ar(plan) else "")
                    else:
                        self._set_engine_mode("")
                    self._pending_loras = plan
                    if on_progress:
                        on_progress(status="Submitting to YuE2", phase="queue")
                    job = session.submit_task(settings, callbacks=callbacks)
                    self.current_job = job
                    try:
                        while not _job_done(job):
                            if cancel is not None and cancel.is_set():
                                job.cancel()
                            time.sleep(0.4)
                        result = job.result()
                    finally:
                        self.current_job = None
                        self._pending_loras = None
            finally:
                self.busy = False
                self.last_used = time.time()
        if not result.success:
            if result.cancelled:
                raise Cancelled("cancelled")
            msg = "; ".join(getattr(e, "message", str(e)) for e in (result.errors or [])) or "generation failed"
            tail = "\n".join(callbacks.lines[-12:])
            raise RuntimeError(msg + ("\n" + tail if tail else ""))
        files = [str(f) for f in (result.generated_files or [])]
        audio = next((f for f in reversed(files) if Path(f).suffix.lower() in AUDIO_EXTS), None)
        if audio is None:
            raise RuntimeError("YuE2 produced no audio file: " + ", ".join(files))
        stem = Path(audio).with_suffix("")
        abc = next((str(p) for p in [Path(str(stem) + ".abc")] if p.exists()), None)
        mid = next((str(p) for p in [Path(str(stem) + ".mid"), Path(str(stem) + ".midi")] if p.exists()), None)
        for side in (Path(audio + ".wangp-files.json"),):   # v18 writes a manifest of the companion files beside the song
            try:
                side.unlink()
            except OSError:
                pass
        if native_loras:
            self.last_lora_report = {**(self.last_lora_report or {}), "native": {Path(k).name: v for k, v in native_loras.items()}}
        plan_info = self.last_plan or {}
        used_seed = None
        try:
            used_seed = int(plan_info.get("request", {}).get("seed"))
        except Exception:
            pass
        return {"audio": audio, "abc": abc, "mid": mid, "plan": plan_info, "lora_report": self.last_lora_report, "seed": used_seed, "files": files,
                "truncated": dict(self.last_truncated or {}), "model_type": model_type}

    # ------------------------------------------------------------------ stems + voice
    def separate(self, audio_path: str | os.PathLike, out_dir: str | os.PathLike, prefix: str = "stem") -> tuple[str, str]:
        """BS-RoFormer split through Wan2GP. Returns (vocals_wav, instrumental_wav)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.busy = True
            try:
                self.session()  # makes sure Wan2GP paths/config are live
                with gpu_lease("tenforward"):
                    from preprocessing.extract_vocals import extract_vocal_and_background_stems  # type: ignore
                    vocals, background = extract_vocal_and_background_stems(str(audio_path), str(out_dir / f"{prefix}_vocals.wav"), str(out_dir / f"{prefix}_instrumental.wav"))
            finally:
                self.busy = False
                self.last_used = time.time()
        return str(vocals), str(background)

    @contextmanager
    def hold(self, label: str = "external"):
        """Mark the engine busy while another GPU job (voice training in a subprocess) runs, so the planner and the idle
        unloader leave the card alone."""
        with self._lock:
            self.busy = True
            self.current_job = label
            try:
                yield
            finally:
                self.busy = False
                self.current_job = None
                self.last_used = time.time()

    def voice_convert(self, vocals_path: str | os.PathLike, voice_sample: str | os.PathLike, out_path: str | os.PathLike, on_progress: Callable[..., None] | None = None,
                      voice: dict[str, Any] | None = None, semi_tone_shift: int = 0, blend: float = 1.0, key_safe: bool = True, ai_samples: dict[str, str] | None = None) -> str:
        """Seed-VC singing conversion. A voice with a trained model (voice_ft) goes through that model: the song is split,
        the vocal converted, the mix rebuilt. Otherwise Wan2GP's zero-shot processor 'seedvc_one_speaker' does the whole song."""
        model = (voice or {}).get("model")
        blend = float(blend if blend is not None else 1.0)
        if model and Path(model).exists() and blend > 0.005:
            return self._voice_convert_trained(vocals_path, voice_sample, out_path, Path(model), on_progress, int(semi_tone_shift or 0), blend, key_safe=key_safe, ai_samples=ai_samples, voice_share=blend)
        # blend 0 on a trained voice: the AI singer only, the base checkpoint with the sample as its prompt (zero-shot), the
        # voice's measured range kept for the octave fit
        voice_pitch = None
        try:
            import json as _json
            meta = (voice or {}).get("train_meta")
            meta = _json.loads(meta) if isinstance(meta, str) and meta else (meta or {})
            voice_pitch = (meta or {}).get("pitch") or None
        except Exception:
            voice_pitch = None
        try:
            import voice_ft  # type: ignore
        except Exception:
            voice_ft = None
        if voice_ft is not None and voice_ft.BASE_CKPT.exists() and voice_ft.PRESET_CONFIG.exists():
            # An untrained voice takes the same road with the base singing checkpoint and its sample as the prompt: the
            # song split, the vocal converted, whole octaves into the sample's measured range (the key stays with the
            # music), the semitone shift honoured. Wan2GP's own processor below shifts the pitch freely to the sample's
            # centre, which changes the key against the instrumental.
            return self._voice_convert_trained(vocals_path, voice_sample, out_path, voice_ft.BASE_CKPT, on_progress, int(semi_tone_shift or 0), 1.0, zero_shot=True, key_safe=key_safe, voice_pitch=voice_pitch,
                                               ai_samples=ai_samples, voice_share=blend if (voice or {}).get("model") else 1.0)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        callbacks = _Callbacks(on_progress)
        with self._lock:
            self.busy = True
            try:
                session = self.session()
                with gpu_lease("tenforward"):
                    job = session.submit_audio_postprocessing(str(vocals_path), postprocess_audio="seedvc_one_speaker", replace_voice_sample=str(voice_sample), callbacks=callbacks)
                    result = job.result()
            finally:
                self.busy = False
                self.last_used = time.time()
        if not result.success:
            msg = "; ".join(getattr(e, "message", str(e)) for e in (result.errors or [])) or "voice conversion failed"
            raise RuntimeError(msg + "\n" + "\n".join(callbacks.lines[-10:]))
        files = [str(f) for f in (result.generated_files or [])]
        src = next((f for f in reversed(files) if Path(f).suffix.lower() in AUDIO_EXTS), None)
        if src is None:
            raise RuntimeError("Seed-VC produced no audio: " + ", ".join(files))
        shutil.move(src, out_path)
        return str(out_path)

    def _voice_convert_trained(self, src: str | os.PathLike, sample: str | os.PathLike, out_path: str | os.PathLike, model: Path, on_progress: Callable[..., None] | None, semi_tone_shift: int, blend: float = 1.0, zero_shot: bool = False, key_safe: bool = True, voice_pitch: dict[str, Any] | None = None,
                               ai_samples: dict[str, str] | None = None, voice_share: float = 1.0) -> str:
        import audio_utils  # type: ignore
        import voice_ft  # type: ignore
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.parent / "_ft"
        tmp.mkdir(parents=True, exist_ok=True)

        def emit(**fields):
            if on_progress:
                try:
                    on_progress(**fields)
                except Exception:
                    LOG.exception("progress callback failed")

        emit(phase="voice", status="Splitting the vocal from the music", frac=0.1)
        vocals, inst = self.separate(src, tmp, prefix="ft")
        with self._lock:
            self.busy = True
            try:
                with gpu_lease("tenforward"):
                    emit(phase="voice", status="Singing it in the voice's own range" if zero_shot else "Singing it through the trained voice", frac=0.4)
                    cfg = voice_ft.base_config() if zero_shot else None
                    pitch = (voice_pitch or voice_ft.analyze_pitch([Path(sample)]) or None) if zero_shot else None   # where the voice sits, for the octave fit
                    conv = voice_ft.convert(vocals, sample, tmp / "ft_converted.wav", ckpt=model, cfg=cfg, semi_tone_shift=semi_tone_shift, blend=blend, key_safe=key_safe, voice_pitch=pitch,
                                            progress=lambda f, s: emit(phase="voice", status=s, frac=0.4 + 0.5 * f), ai_samples=ai_samples, voice_share=voice_share)
            finally:
                self.busy = False
                self.last_used = time.time()
        emit(phase="voice", status="Mixing the new vocal back in", frac=0.9)
        audio_utils.mix_vocals_instrumental(conv, inst, out_path)
        try:
            shutil.move(str(conv), str(out_path.with_name("vocals.wav")))   # the new voice alone, kept beside the mix: hear it dry
        except Exception:
            LOG.exception("could not keep the converted vocal")
        shutil.rmtree(tmp, ignore_errors=True)
        return str(out_path)

    # ------------------------------------------------------------------ the sheet of a recording (1.6)
    SCORING_CKPT = WANGP_ROOT / "ckpts" / "sheetsage2" / "SheetSage2_MERT2_bf16.safetensors"

    def _patch_scoring(self):
        """Keep the whole SheetSage2 result (section times, chords, key, beats) of the last transcription, not only
        the ABC and MIDI that score_audio hands back."""
        from models.TTS.yue2.sheetsage2 import pipeline_sheetsage2 as pss  # type: ignore
        if getattr(pss.Transcriber, "_tf_patched", False):
            return
        original = pss.Transcriber.analyze
        engine = self

        def analyze(transcriber, *args, **kwargs):
            res = original(transcriber, *args, **kwargs)
            engine.last_score = res
            return res

        pss.Transcriber.analyze = analyze
        pss.Transcriber._tf_patched = True

    @staticmethod
    def _rows(text: str | None) -> list[list[Any]]:
        out = []
        for line in (text or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    out.append([float(parts[0]), float(parts[1]), parts[2].strip()])
                except ValueError:
                    pass
        return out

    def score(self, audio_path: str | os.PathLike, melody_only: bool = False, on_progress: Callable[..., None] | None = None,
              cancel: threading.Event | None = None) -> dict[str, Any]:
        """SheetSage2 reads a recording into a lead sheet (the same call a cover makes, without the song after it).
        Returns {abc, midi (bytes), structure [[start, end, label]], chords, keys, beats, duration_s, warnings}."""
        if not self.SCORING_CKPT.exists():
            raise RuntimeError("the SheetSage2 transcription model is not installed")
        self.last_score = None

        def cb(**k):
            if on_progress:
                try:
                    on_progress(phase="score", status=str(k.get("progress_title") or k.get("denoising_extra") or "Writing the score"),
                                step=k.get("step_idx"), total=k.get("override_num_inference_steps"))
                except Exception:
                    LOG.exception("progress callback failed")

        def abort() -> bool:
            return bool(cancel is not None and cancel.is_set())

        with self._lock:
            self.busy = True
            self.current_job = "score"
            self.last_used = time.time()
            try:
                self.session()
                self._patch_scoring()
                with gpu_lease("tenforward"):
                    self.comfy_free_if_idle()
                    from models.TTS.yue2.sheetsage2.scoring import score_audio  # type: ignore
                    abc, midi = score_audio(str(audio_path), str(self.SCORING_CKPT), bool(melody_only), cb, abort)
            except InterruptedError:
                raise Cancelled("cancelled")
            finally:
                self.busy = False
                self.current_job = None
                self.last_used = time.time()
        res = getattr(self, "last_score", None) or {}
        labs = res.get("labs") or {}
        return {"abc": abc, "midi": midi, "structure": self._rows(labs.get("structure")), "chords": self._rows(labs.get("chord")),
                "keys": self._rows(labs.get("key")), "beats": labs.get("beat") or "", "duration_s": res.get("duration_seconds"),
                "warnings": list(res.get("warnings") or [])}

    # ------------------------------------------------------------------ whisper
    def transcribe(self, audio_path: str | os.PathLike, language: str | None = None) -> dict[str, Any]:
        """Whisper transcription. Returns {text, lines[], segments[{start, end, text}], language}."""
        import whisper  # type: ignore
        with self._lock:
            self.busy = True
            try:
                if NATIVE_LM:
                    self.session()    # v18: Wan2GP installs its VRAM allocator on first load; whisper must not be the first thing to touch CUDA
                with gpu_lease("tenforward"):
                    if self._whisper is None:
                        self._whisper = whisper.load_model(WHISPER_MODEL, device="cuda")
                    res = self._whisper.transcribe(str(audio_path), language=language, fp16=True, verbose=False)
            finally:
                self.busy = False
                self.last_used = time.time()
        lines = [seg.get("text", "").strip() for seg in res.get("segments", []) if seg.get("text", "").strip()]
        segments = [{"start": round(float(seg.get("start") or 0), 2), "end": round(float(seg.get("end") or 0), 2), "text": seg.get("text", "").strip()}
                    for seg in res.get("segments", []) if seg.get("text", "").strip()]
        return {"text": res.get("text", "").strip(), "lines": lines, "segments": segments, "language": res.get("language")}

    def _free_whisper(self):
        if self._whisper is not None:
            self._whisper = None


ENGINE: Engine | None = None


def get_engine() -> Engine:
    global ENGINE
    if ENGINE is None:
        ENGINE = Engine()
    return ENGINE
