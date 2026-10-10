"""
loras.py — runtime LoRA support for YuE2 inside Wan2GP.

Community YuE2 LoRAs (Mothersuperior et al.) ship as safetensors with keys like

    layers.{i}.nar_self_attn.q_proj.lora_A   [rank, in]      (acoustic / NAR branch)
    layers.{i}.nar_self_attn.q_proj.lora_B   [out, rank]
    layers.{i}.nar_mlp.gate_proj.lora_A / lora_B
    layers.{i}.self_attn.q_proj.lora_A / lora_B                (planner / AR branch)
    layers.{i}.mlp.up_proj.lora_A / lora_B
    vae2llm.weight / vae2llm.bias / llm2vae.weight / llm2vae.bias   (FULL replacement weights)

Wan2GP splits the model into `pipeline.text_encoder` (AR, Qwen3ForCausalLM: .model.layers[i].self_attn.*)
and `pipeline.transformer` (YuE2Acoustic: .model.layers[i].nar_self_attn.*, .vae2llm, .llm2vae).
The base weights are INT8 (QLinearInt8ConvRot subclasses nn.Linear), so we never fold deltas into
weights. Instead each target Linear gets a forward hook adding  scale * (x @ A.T) @ B.T .
Full replacement weights are swapped in (and restored afterwards) when dtype/shape allow.

Hooks are applied for one job and removed afterwards, so any combination of LoRAs and scales can be
used per song without reloading the model.

Limitation: the AR branch under Wan2GP's CUDA-graph LM engine replays captured graphs, so Python
hooks are bypassed there. engine.py switches the LM engine to "legacy" for jobs that use an AR LoRA.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import torch
from safetensors import safe_open

TF_ROOT = Path(__file__).resolve().parent
LORA_DIR = TF_ROOT / "loras"
_META_FILE = LORA_DIR / "loras.json"

_KEY_RE = re.compile(r"^(?:model\.)?layers\.(\d+)\.(nar_self_attn|nar_mlp|self_attn|mlp)\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)\.(lora_A|lora_B|lora_down|lora_up)(?:\.weight)?$")
_FULL_RE = re.compile(r"^(?:model\.)?(vae2llm|llm2vae)\.(weight|bias)$")

# Curated descriptions for the LoRAs we ship. Anything else found in loras/ is listed generically.
KNOWN = {
    "nar_lora_joint_v4.bf16.safetensors": {
        "name": "Real-audio decoder (Mothersuperior v4)",
        "kind": "nar",
        "blurb": "Makes the acoustic decoder render like real productions instead of YuE2's own house sound. Good default for covers and voice work.",
        "default_scale": 1.0,
        "trigger": "",
        "source": "https://huggingface.co/Mothersuperior/yue2-mothersuperior-realaudio-tokenizer-v4",
    },
    "ar_lora_inst_v3abc.bf16.safetensors": {
        "name": "Instrumental planner (Mothersuperior v3)",
        "kind": "ar",
        "blurb": "Planner LoRA for instrumental tracks with section tags. Use lyrics like [intro] [verse] [chorus] [outro] on their own lines, or just [instrumental]. Works best with Melody and chords planning.",
        "default_scale": 1.0,
        "trigger": "[instrumental]",
        "source": "https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras",
    },
    "hum_adapter_v1_combined.safetensors": {
        "name": "Hum adapter (experimental)",
        "kind": "nar",
        "blurb": "Rank-96 decoder LoRA from the hum-to-song adapter. Only the LoRA part is applied here (the hum carrier projections are not wired), so treat it as an alternative decoder flavour.",
        "default_scale": 0.7,
        "trigger": "",
        "source": "https://huggingface.co/Mothersuperior/YuE2-hum-to-song",
    },
}


def _load_meta() -> dict[str, Any]:
    if _META_FILE.exists():
        try:
            return json.loads(_META_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def scan() -> list[dict[str, Any]]:
    """List LoRA files with metadata (kind detection reads only the key names)."""
    LORA_DIR.mkdir(parents=True, exist_ok=True)
    user_meta = _load_meta()
    out = []
    for path in sorted(LORA_DIR.glob("*.safetensors")):
        info = dict(KNOWN.get(path.name, {}))
        info.update(user_meta.get(path.name, {}))
        kind = info.get("kind")
        n_pairs = 0
        unknown = 0
        full = 0
        try:
            with safe_open(str(path), framework="pt", device="cpu") as f:
                keys = list(f.keys())
            ar = nar = 0
            for k in keys:
                m = _KEY_RE.match(k)
                if m:
                    if m.group(2).startswith("nar_"):
                        nar += 1
                    else:
                        ar += 1
                elif _FULL_RE.match(k):
                    full += 1
                else:
                    unknown += 1
            n_pairs = (ar + nar) // 2
            if not kind:
                kind = "nar" if nar >= ar else "ar"
            if ar and nar:
                kind = "both"
        except Exception as e:  # unreadable file
            info["error"] = str(e)
        out.append({
            "file": path.name,
            "name": info.get("name", path.stem),
            "kind": kind or "unknown",
            "blurb": info.get("blurb", ""),
            "default_scale": float(info.get("default_scale", 1.0)),
            "trigger": info.get("trigger", ""),
            "source": info.get("source", ""),
            "size_mb": round(path.stat().st_size / 1e6, 1),
            "pairs": n_pairs,
            "full_replacements": full,
            "unknown_keys": unknown,
            "error": info.get("error"),
        })
    return out


class _LoraHook:
    """Forward hook adding scale * (x @ A.T) @ B.T to a Linear's output."""

    def __init__(self, A: torch.Tensor, B: torch.Tensor, scale: float):
        self.A = A  # [rank, in]
        self.B = B  # [out, rank]
        self.scale = float(scale)
        self._cache: dict[tuple, tuple[torch.Tensor, torch.Tensor]] = {}
        self.calls = 0

    def _ab(self, device, dtype):
        key = (str(device), dtype)
        ab = self._cache.get(key)
        if ab is None:
            ab = (self.A.to(device=device, dtype=dtype), self.B.to(device=device, dtype=dtype))
            self._cache[key] = ab
        return ab

    def __call__(self, module, inputs, output):
        x = inputs[0]
        if x is None:
            return output
        A, B = self._ab(x.device, x.dtype if x.dtype in (torch.bfloat16, torch.float16, torch.float32) else torch.bfloat16)
        if x.dtype != A.dtype:
            x = x.to(A.dtype)
        delta = torch.matmul(torch.matmul(x, A.t()), B.t())
        if self.scale != 1.0:
            delta = delta * self.scale
        self.calls += 1
        if output.dtype != delta.dtype:
            delta = delta.to(output.dtype)
        return output + delta


class _FullReplaceHook:
    """Forward hook that recomputes a Linear layer from replacement weights (ignores the module's output)."""

    def __init__(self, weight: torch.Tensor, bias: torch.Tensor | None):
        self.W = weight
        self.b = bias
        self._cache: dict[tuple, tuple[torch.Tensor, torch.Tensor | None]] = {}
        self.calls = 0

    def __call__(self, module, inputs, output):
        x = inputs[0]
        if x is None:
            return output
        dtype = x.dtype if x.dtype in (torch.bfloat16, torch.float16, torch.float32) else torch.bfloat16
        key = (str(x.device), dtype)
        wb = self._cache.get(key)
        if wb is None:
            wb = (self.W.to(device=x.device, dtype=dtype), self.b.to(device=x.device, dtype=dtype) if self.b is not None else None)
            self._cache[key] = wb
        self.calls += 1
        y = torch.nn.functional.linear(x.to(dtype), wb[0], wb[1])
        return y.to(output.dtype) if output.dtype != y.dtype else y


class LoraSession:
    """Apply a set of LoRAs (file -> scale) onto a YuE2 pipeline; call .remove() afterwards."""

    def __init__(self):
        self.handles: list[Any] = []
        self.hooks: list[_LoraHook] = []
        self.restores: list[tuple[torch.nn.Module, str, torch.Tensor]] = []
        self.report: dict[str, Any] = {"applied": [], "skipped": [], "unknown_keys": 0}
        self._lock = threading.Lock()

    @staticmethod
    def _find_module(root: torch.nn.Module, layer: int, block: str, proj: str):
        try:
            layers = root.model.layers
        except AttributeError:
            return None
        if layer >= len(layers):
            return None
        blk = getattr(layers[layer], block, None)
        if blk is None:
            return None
        return getattr(blk, proj, None)

    def apply(self, pipeline, plan: dict[str, float]) -> dict[str, Any]:
        """plan: {filename: scale}. Returns a report dict."""
        text_encoder = getattr(pipeline, "text_encoder", None)
        transformer = getattr(pipeline, "transformer", None)
        for fname, scale in plan.items():
            path = LORA_DIR / fname
            if not path.exists() or scale == 0:
                self.report["skipped"].append({"file": fname, "reason": "missing" if not path.exists() else "scale 0"})
                continue
            pairs: dict[str, dict[str, torch.Tensor]] = {}
            fulls: dict[str, torch.Tensor] = {}
            with safe_open(str(path), framework="pt", device="cpu") as f:
                for k in f.keys():
                    m = _KEY_RE.match(k)
                    if m:
                        layer, block, proj, which = m.groups()
                        key = f"{layer}.{block}.{proj}"
                        side = "A" if which in ("lora_A", "lora_down") else "B"
                        pairs.setdefault(key, {})[side] = f.get_tensor(k)
                        continue
                    m2 = _FULL_RE.match(k)
                    if m2:
                        fulls[f"{m2.group(1)}.{m2.group(2)}"] = f.get_tensor(k)
                        continue
                    self.report["unknown_keys"] += 1
            applied = 0
            missing_modules = 0
            for key, ab in pairs.items():
                if "A" not in ab or "B" not in ab:
                    continue
                layer_s, block, proj = key.split(".")
                root = transformer if block.startswith("nar_") else text_encoder
                if root is None:
                    missing_modules += 1
                    continue
                mod = self._find_module(root, int(layer_s), block, proj)
                if mod is None:
                    missing_modules += 1
                    continue
                A, B = ab["A"], ab["B"]
                # Guard against orientation mistakes: A must be [rank, in], B [out, rank].
                if A.shape[1] != getattr(mod, "in_features", A.shape[1]):
                    if A.shape[0] == getattr(mod, "in_features", -1):
                        A = A.t().contiguous()
                if B.shape[0] != getattr(mod, "out_features", B.shape[0]):
                    if B.shape[1] == getattr(mod, "out_features", -1):
                        B = B.t().contiguous()
                hook = _LoraHook(A.to(torch.bfloat16), B.to(torch.bfloat16), scale)
                self.handles.append(mod.register_forward_hook(hook))
                self.hooks.append(hook)
                applied += 1
            # Full replacement layers (vae2llm / llm2vae): never touch the module's own weights, which
            # mmgp keeps offloaded (writing to them segfaults). A forward hook recomputes the layer
            # from the replacement weights instead.
            replaced = 0
            by_module: dict[str, dict[str, torch.Tensor]] = {}
            for key, tensor in fulls.items():
                name, attr = key.split(".")
                by_module.setdefault(name, {})[attr] = tensor
            for name, parts in by_module.items():
                mod = getattr(transformer, name, None) if transformer is not None else None
                if mod is None or "weight" not in parts:
                    self.report["skipped"].append({"file": fname, "reason": f"{name}: module not found or weight missing"})
                    continue
                w = parts["weight"]
                if (getattr(mod, "out_features", w.shape[0]), getattr(mod, "in_features", w.shape[1])) != tuple(w.shape):
                    self.report["skipped"].append({"file": fname, "reason": f"{name}: shape mismatch {tuple(w.shape)}"})
                    continue
                hook = _FullReplaceHook(w.to(torch.bfloat16), parts.get("bias").to(torch.bfloat16) if parts.get("bias") is not None else None)
                self.handles.append(mod.register_forward_hook(hook))
                self.hooks.append(hook)
                replaced += 1
            self.report["applied"].append({"file": fname, "scale": scale, "hooks": applied, "missing_modules": missing_modules, "replaced": replaced})
        return self.report

    def calls(self) -> int:
        return sum(h.calls for h in self.hooks)

    def remove(self):
        with self._lock:
            for h in self.handles:
                try:
                    h.remove()
                except Exception:
                    pass
            self.handles.clear()
            with torch.no_grad():
                for mod, attr, original in self.restores:
                    try:
                        getattr(mod, attr).data.copy_(original.to(getattr(mod, attr).data.device))
                    except Exception:
                        pass
            self.restores.clear()


def uses_ar(plan: dict[str, float]) -> bool:
    """True if any LoRA in the plan touches the AR / planner branch."""
    kinds = {l["file"]: l["kind"] for l in scan()}
    return any(kinds.get(f) in ("ar", "both") for f, s in plan.items() if s)
