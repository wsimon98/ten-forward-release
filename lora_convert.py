"""lora_convert.py — community and home-trained YuE2 LoRAs into the one layout loras.py reads.

YuE2 LoRAs come in three layouts:

  * Mothersuperior / Wan2GP (what loras.py reads):  layers.N.nar_self_attn.q_proj.lora_A   (decoder, "sound" half)
                                                    layers.N.self_attn.q_proj.lora_A       (planner half)
  * ComfyUI fused (Ostris AI Toolkit, most community files):
        diffusion_model.model.layers.N.self_attn.qkv_proj.lora_A.weight   q, k and v in ONE matrix (2048+1024+1024 rows)
        diffusion_model.model.layers.N.mlp.gate_up_proj.lora_B.weight     gate and up in one (6144+6144 rows)
        text_encoders.model.layers.N...                                    the planner half
  * PEFT adapters (one half per file, e.g. adapter-nar-194/lora.safetensors):
        base_model.model.model.layers.N.self_attn.q_proj.lora_A.weight

The fused matrices are split exactly (A is shared, B is cut by rows), names are mapped, an `alpha` is folded into B
(alpha / rank), and the result is written to loras/<name>.safetensors. When a LoRA carries both halves, the decoder
half alone is also written as loras/<name>.sound.safetensors: the planner half forces Wan2GP's slow legacy engine, the
sound half alone plays at normal speed. Each file is registered in loras/loras.json (name, kind, trigger, scale, source).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import torch
from safetensors import safe_open
from safetensors.torch import save_file

import loras

_PAIR = re.compile(r"(?:^|\.)layers\.(\d+)\.(nar_self_attn|nar_mlp|self_attn|mlp)\."
                   r"(q_proj|k_proj|v_proj|o_proj|qkv_proj|gate_proj|up_proj|down_proj|gate_up_proj)\."
                   r"(lora_A|lora_B|lora_down|lora_up|alpha)(?:\.weight)?$")
_FULL = re.compile(r"(?:^|\.)(vae2llm|llm2vae)\.(weight|bias)$")
# a LoRA on the two layers that bridge the decoder and the audio (some decoder LoRAs train them too)
_IO = re.compile(r"(?:^|\.)(vae2llm|llm2vae)\.(lora_A|lora_B|lora_down|lora_up|alpha)(?:\.weight)?$")
# ComfyUI "diff" patches on those layers: the change to ADD to the base weight / bias (AI Toolkit writes these)
_DIFF = re.compile(r"(?:^|\.)(vae2llm|llm2vae)\.(diff|diff_b)$")
_DECODER_PREFIX = ("diffusion_model.", "transformer.", "model.diffusion_model.")
_PLANNER_PREFIX = ("text_encoders.", "text_encoder.", "model.text_encoder.")


def _half(key: str, block: str, hint: str | None) -> str:
    if block.startswith("nar_"):
        return "nar"
    if key.startswith(_DECODER_PREFIX):
        return "nar"
    if key.startswith(_PLANNER_PREFIX):
        return "ar"
    return hint or "ar"


def read(src: Path, half_hint: str | None = None) -> tuple[dict[str, torch.Tensor], dict[str, Any]]:
    """Any of the three layouts in, loras.py's layout out (plus a little report)."""
    raw: dict[tuple, dict[str, torch.Tensor]] = {}
    alphas: dict[tuple, float] = {}
    fulls: dict[str, torch.Tensor] = {}
    unknown = []
    with safe_open(str(src), framework="pt", device="cpu") as f:
        for k in f.keys():
            m = _PAIR.search(k)
            if m:
                layer, block, proj, which = m.groups()
                half = _half(k, block, half_hint)
                attn = "attn" in block
                slot = (half, int(layer), "self_attn" if attn else "mlp", proj)
                if which == "alpha":
                    alphas[slot] = float(f.get_tensor(k).float().item())
                else:
                    raw.setdefault(slot, {})["A" if which in ("lora_A", "lora_down") else "B"] = f.get_tensor(k)
                continue
            m2 = _FULL.search(k)
            if m2:
                fulls[f"{m2.group(1)}.{m2.group(2)}"] = f.get_tensor(k)
                continue
            m4 = _DIFF.search(k)
            if m4:
                fulls[f"{m4.group(1)}.{'delta_weight' if m4.group(2) == 'diff' else 'delta_bias'}"] = f.get_tensor(k)
                continue
            m3 = _IO.search(k)
            if m3:
                slot = ("io", 0, m3.group(1), "")
                if m3.group(2) == "alpha":
                    alphas[slot] = float(f.get_tensor(k).float().item())
                else:
                    raw.setdefault(slot, {})["A" if m3.group(2) in ("lora_A", "lora_down") else "B"] = f.get_tensor(k)
                continue
            unknown.append(k)
    out: dict[str, torch.Tensor] = {}

    def put(half: str, layer: int, block: str, proj: str, A: torch.Tensor, B: torch.Tensor):
        name = f"layers.{layer}.{'nar_' if half == 'nar' else ''}{block}.{proj}"
        # clone: a fused qkv / gate_up shares one A, and safetensors refuses tensors that share memory
        out[name + ".lora_A"] = A.to(torch.bfloat16).contiguous().clone()
        out[name + ".lora_B"] = B.to(torch.bfloat16).contiguous().clone()

    for slot, ab in raw.items():
        if "A" not in ab or "B" not in ab:
            continue
        half, layer, block, proj = slot
        A, B = ab["A"], ab["B"]
        if A.shape[0] > A.shape[1] and B.shape[1] > B.shape[0]:      # stored transposed
            A, B = A.t(), B.t()
        rank = A.shape[0]
        if slot in alphas and rank:
            B = B * (alphas[slot] / rank)
        if half == "io":
            out[f"{block}.lora_A"] = A.to(torch.bfloat16).contiguous()
            out[f"{block}.lora_B"] = B.to(torch.bfloat16).contiguous()
            continue
        if proj == "qkv_proj":
            q = A.shape[1]                                            # q is as wide as the hidden size
            kv = (B.shape[0] - q) // 2
            put(half, layer, block, "q_proj", A, B[:q])
            put(half, layer, block, "k_proj", A, B[q:q + kv])
            put(half, layer, block, "v_proj", A, B[q + kv:])
        elif proj == "gate_up_proj":
            h = B.shape[0] // 2
            put(half, layer, block, "gate_proj", A, B[:h])
            put(half, layer, block, "up_proj", A, B[h:])
        else:
            put(half, layer, block, proj, A, B)
    for k, t in fulls.items():
        out[k] = t.to(torch.bfloat16).contiguous()
    io = sum(1 for k in out if k.startswith(("vae2llm.lora", "llm2vae.lora"))) // 2
    nar = sum(1 for k in out if ".nar_" in k) // 2 + io        # the bridge layers belong to the decoder
    ar = sum(1 for k in out if k.startswith("layers.") and ".nar_" not in k) // 2
    return out, {"nar_pairs": nar, "ar_pairs": ar, "io_pairs": io, "full": len(fulls), "unknown": unknown[:10], "unknown_count": len(unknown)}


def _register(fname: str, meta: dict[str, Any]) -> None:
    data = loras._load_meta()
    data[fname] = {k: v for k, v in meta.items() if v not in (None, "")}
    loras.LORA_DIR.mkdir(parents=True, exist_ok=True)
    loras._META_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def convert_file(src: Path, name: str, trigger: str = "", default_scale: float | None = None, source: str = "",
                 blurb: str = "", half_hint: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Convert one LoRA file into loras/<name>.safetensors (+ <name>.sound.safetensors when it has both halves).
    `half_hint` ('nar' / 'ar') says which half a PEFT adapter file is, when its keys cannot tell."""
    src = Path(src)
    loras.LORA_DIR.mkdir(parents=True, exist_ok=True)       # a fresh install has no loras/ yet
    tensors, rep = read(src, half_hint)
    if not (rep["nar_pairs"] or rep["ar_pairs"] or rep["full"]):
        raise ValueError(f"{src.name}: no LoRA weights in a layout this knows ({rep['unknown_count']} other keys, e.g. {rep['unknown'][:3]})")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "lora"
    kind = "both" if rep["nar_pairs"] and rep["ar_pairs"] else ("nar" if rep["nar_pairs"] or rep["full"] else "ar")
    files = []
    title = title or name.replace("_", " ")
    main = f"{safe}.safetensors"
    save_file(tensors, str(loras.LORA_DIR / main), metadata={"source": source or src.name, "converted_by": "ten-forward lora_convert"})
    scale = default_scale if default_scale is not None else (0.8 if kind in ("ar", "both") else 1.0)
    _register(main, {"name": title + (" (planner + sound)" if kind == "both" else ""), "kind": kind, "trigger": trigger,
                     "default_scale": scale, "source": source, "blurb": blurb})
    files.append(main)
    if kind == "both":
        sound = {k: v for k, v in tensors.items() if ".nar_" in k or k.startswith(("vae2llm.", "llm2vae."))}
        sname = f"{safe}.sound.safetensors"
        save_file(sound, str(loras.LORA_DIR / sname), metadata={"source": source or src.name, "half": "sound (decoder) only"})
        _register(sname, {"name": title + " (sound half, full speed)", "kind": "nar", "trigger": trigger,
                          "default_scale": default_scale if default_scale is not None else 1.0, "source": source,
                          "blurb": (blurb + " " if blurb else "") + "The sound half alone: plays at normal speed."})
        files.append(sname)
        write_plan_half(main, tensors, source or src.name)
    return {"files": files, "kind": kind, **rep}


def write_plan_half(main: str, tensors: dict[str, torch.Tensor] | None = None, source: str = "") -> str | None:
    """The planner half of a two-half LoRA alone, as loras/_plan/<name>.plan.safetensors. Wan2GP 18 runs the planner
    on its own fast engine, where only its own loader can reach it: engine.py hands it this file and keeps the sound
    half on Ten Forward's hooks. Kept in a sub-folder so it does not show up as one more LoRA in the lists."""
    if tensors is None:
        from safetensors.torch import load_file
        tensors = load_file(str(loras.LORA_DIR / main))
    plan = {k: v for k, v in tensors.items() if k.startswith("layers.") and ".nar_" not in k}
    if not plan:
        return None
    d = loras.LORA_DIR / "_plan"
    d.mkdir(exist_ok=True)
    out = d / (Path(main).stem + ".plan.safetensors")
    save_file({k: v.contiguous().clone() for k, v in plan.items()}, str(out), metadata={"source": source, "half": "planner only"})
    return out.name


def merge_halves(planner: Path, sound: Path, name: str, **meta) -> dict[str, Any]:
    """A LoRA shipped as two PEFT adapters (planner file + sound file) into one, plus its sound half alone."""
    a, ra = read(Path(planner), "ar")
    b, rb = read(Path(sound), "nar")
    tmp = loras.LORA_DIR / f"_{name}_merge.tmp.safetensors"
    save_file({**a, **b}, str(tmp))
    try:
        return convert_file(tmp, name, **meta)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    # python lora_convert.py <lora.safetensors> [name] [--trigger WORD] [--scale 0.8] [--half ar|nar]
    import argparse
    ap = argparse.ArgumentParser(description="Turn a YuE2 LoRA (ComfyUI / AI Toolkit / PEFT) into loras/<name>.safetensors")
    ap.add_argument("file")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--trigger", default="", help="the LoRA's trigger word, if its page names one")
    ap.add_argument("--scale", type=float, default=None, help="default strength")
    ap.add_argument("--half", choices=["ar", "nar"], default=None, help="which half a one-half PEFT adapter is")
    a = ap.parse_args()
    src = Path(a.file)
    out = convert_file(src, a.name or src.stem, trigger=a.trigger, default_scale=a.scale, source=src.name, half_hint=a.half)
    print(json.dumps(out, indent=2, default=str))
