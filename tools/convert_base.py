"""Turn a whole YuE2 model in ComfyUI's one-file layout (a finetune such as YuE2-RS, int8 ConvRot or bf16) into
the two files Wan2GP 18 loads, plus a finetune entry so Ten Forward can pick it as its music model.

    python tools\\convert_base.py <comfyui checkpoint> <id> "<title>" <short name>

<short name> (lower case, e.g. rs) is what Settings > Music model lists.

Wan2GP's own stock files are the template: every tensor they hold is looked up in the finetune (names mapped,
ComfyUI's fused q/k/v and gate/up matrices cut by rows - exact, the int8 scales are per row and the ConvRot
rotation runs along the input side), and where Wan2GP keeps a tensor in bf16 but the finetune is int8 it is
dequantized (int8 x row scale, then the Hadamard rotation undone). Check printed at the end: the finetune's weights
must sit near the stock ones (a merged LoRA moves them a little); a wrong name or a wrong cut would be far off.

Writes  <ckpts>/YuE2_<id>_Acoustic_int8_convrot.safetensors
        <ckpts>/YuE2_AR/YuE2_<id>_AR_int8_convrot.safetensors
        <wan2gp>/finetunes/yue2_<short name>.json   (model type "yue2_<short name>")
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file

TF_ROOT = Path(__file__).resolve().parent.parent
WANGP = Path(os.environ.get("TF_WANGP_ROOT") or (TF_ROOT / "wan2gp"))   # the same engine tree engine.py uses
CKPTS = WANGP / "ckpts"
STOCK_ACOUSTIC = CKPTS / "YuE2_Acoustic_int8_convrot.safetensors"
STOCK_AR = CKPTS / "YuE2_AR" / "YuE2_AR_int8_convrot.safetensors"

_H: dict[int, torch.Tensor] = {}


def hadamard(n: int) -> torch.Tensor:
    if n not in _H:
        h4 = torch.tensor([[1, 1, 1, -1], [1, 1, -1, 1], [1, -1, 1, 1], [-1, 1, 1, 1]], dtype=torch.float32)
        h = h4
        while h.shape[0] < n:
            h = torch.kron(h, h4)
        _H[n] = h * (n ** -0.5)
    return _H[n]


def dequant(q: torch.Tensor, scale: torch.Tensor, cfg: dict) -> torch.Tensor:
    w = q.float() * scale.float()
    if cfg.get("convrot"):
        g = int(cfg.get("convrot_groupsize") or 256)
        w = (w.reshape(w.shape[0], -1, g) @ hadamard(g)).reshape(w.shape)
    return w.to(torch.bfloat16)


def _src_name(stock_key: str, half: str) -> str:
    """The finetune's name for a stock tensor (before any fused cut)."""
    if half == "acoustic":
        k = (stock_key.replace("nar_input_layernorm", "input_layernorm").replace("nar_pre_mlp_layernorm", "post_attention_layernorm")
             .replace("nar_self_attn", "self_attn").replace("nar_mlp", "mlp"))
        return "model.diffusion_model." + k
    return "text_encoders." + (stock_key if stock_key.startswith("model.") else "model." + stock_key)


_FUSED = {"q_proj": ("qkv_proj", 0), "k_proj": ("qkv_proj", 1), "v_proj": ("qkv_proj", 2),
          "gate_proj": ("gate_up_proj", 0), "up_proj": ("gate_up_proj", 1)}


class Source:
    def __init__(self, path: Path):
        self.f = safe_open(str(path), framework="pt", device="cpu")
        self.keys = set(self.f.keys())
        self.meta = self.f.metadata() or {}

    def get(self, name: str) -> torch.Tensor:
        return self.f.get_tensor(name)

    def cfg(self, module: str) -> dict:
        k = module + ".comfy_quant"
        if k not in self.keys:
            return {}
        return json.loads(bytes(self.get(k).tolist()).decode("utf-8"))

    def fetch(self, name: str) -> tuple[torch.Tensor, str]:
        """Tensor for a (mapped) name, cutting it out of a fused matrix when needed. Returns (tensor, kind) with
        kind 'plain' or 'fused'."""
        if name in self.keys:
            return self.get(name), "plain"
        m = re.match(r"^(.*)\.(q_proj|k_proj|v_proj|gate_proj|up_proj)\.(weight|weight_scale|comfy_quant|bias)$", name)
        if not m:
            raise KeyError(name)
        base, proj, suffix = m.groups()
        fused, idx = _FUSED[proj]
        fk = f"{base}.{fused}.{suffix}"
        if fk not in self.keys:
            raise KeyError(name)
        t = self.get(fk)
        if suffix == "comfy_quant":
            return t, "fused"
        if fused == "qkv_proj":
            q_rows = t.shape[0] // 2          # q is as wide as the hidden size; k and v share the other half
            kv = q_rows // 2
            cuts = [(0, q_rows), (q_rows, q_rows + kv), (q_rows + kv, t.shape[0])]
        else:
            h = t.shape[0] // 2
            cuts = [(0, h), (h, t.shape[0])]
        a, b = cuts[idx]
        return t[a:b].contiguous().clone(), "fused"


def convert_half(src: Source, stock_path: Path, half: str) -> tuple[dict[str, torch.Tensor], dict, dict]:
    out: dict[str, torch.Tensor] = {}
    report = {"copied": 0, "dequantized": 0, "rel_err_max": 0.0, "rel_err_mean": 0.0, "checked": 0, "same": 0}
    errs = []
    with safe_open(str(stock_path), framework="pt", device="cpu") as stock:
        skeys = list(stock.keys())
        smeta = stock.metadata() or {}
        for key in skeys:
            if key.endswith((".comfy_quant", ".weight_scale")):
                continue                                        # handled with their weight
            st = stock.get_slice(key)
            sdtype = st.get_dtype()
            name = _src_name(key, half)
            module = key[: -len(".weight")] if key.endswith(".weight") else None
            stock_quant = module is not None and (module + ".comfy_quant") in skeys
            t, _kind = src.fetch(name)
            src_module = name[: -len(".weight")] if name.endswith(".weight") else None
            src_quant = t.dtype == torch.int8 and src_module is not None
            if stock_quant:
                if not src_quant:
                    raise RuntimeError(f"{key}: stock is int8, the finetune is not ({t.dtype}); not handled")
                out[key] = t
                out[module + ".weight_scale"] = src.fetch(src_module + ".weight_scale")[0]
                out[module + ".comfy_quant"] = src.fetch(src_module + ".comfy_quant")[0]
                report["copied"] += 1
                ref = stock.get_tensor(key)
                if ref.shape != t.shape:
                    raise RuntimeError(f"{key}: shape {tuple(t.shape)} vs stock {tuple(ref.shape)}")
                # compare in the quantized domain, row-scaled (both share the rotation)
                a = ref.float() * stock.get_tensor(module + ".weight_scale").float()
                b = t.float() * out[module + ".weight_scale"].float()
            else:
                if src_quant:
                    cfg = src.cfg(src_module) if (src_module + ".comfy_quant") in src.keys else json.loads(bytes(src.fetch(src_module + ".comfy_quant")[0].tolist()))
                    t = dequant(t, src.fetch(src_module + ".weight_scale")[0], cfg)
                    report["dequantized"] += 1
                else:
                    report["copied"] += 1
                if sdtype == "BF16":
                    t = t.to(torch.bfloat16)
                ref = stock.get_tensor(key)
                if ref.shape != t.shape:
                    raise RuntimeError(f"{key}: shape {tuple(t.shape)} vs stock {tuple(ref.shape)}")
                out[key] = t.contiguous()
                a, b = ref.float(), t.float()
            if a.numel() > 1:
                d = float((a - b).norm() / (a.norm() + 1e-12))
                errs.append(d)
                report["same"] += int(d == 0.0)
    report["checked"] = len(errs)
    report["rel_err_max"] = round(max(errs), 4) if errs else 0.0
    report["rel_err_mean"] = round(sum(errs) / len(errs), 4) if errs else 0.0
    missing = [k for k in skeys if k not in out]
    if missing:
        raise RuntimeError(f"{half}: {len(missing)} stock tensors not produced, e.g. {missing[:5]}")
    return out, smeta, report


def main(src_path: str, ident: str, title: str, short: str) -> None:
    short = re.sub(r"[^a-z0-9_]+", "_", short.lower()).strip("_")
    if not short or short in ("stock", "hum"):
        raise SystemExit("the short name must be lower-case letters/digits and not stock or hum")
    ident = re.sub(r"[^A-Za-z0-9_.-]+", "_", ident).strip("._")
    src = Source(Path(src_path))
    meta_src = {k: v for k, v in src.meta.items() if k.startswith(("ruach.", "modelspec.", "license", "source"))}
    ac_name = f"YuE2_{ident}_Acoustic_int8_convrot.safetensors"
    ar_name = f"YuE2_{ident}_AR_int8_convrot.safetensors"
    for half, stock_path, dest in (("acoustic", STOCK_ACOUSTIC, CKPTS / ac_name),
                                   ("ar", STOCK_AR, CKPTS / "YuE2_AR" / ar_name)):
        tensors, smeta, rep = convert_half(src, stock_path, half)
        md = {**smeta, **{k: str(v) for k, v in meta_src.items()}, "converted_by": "ten-forward convert_base", "from": Path(src_path).name}
        save_file({k: v.contiguous().clone() for k, v in tensors.items()}, str(dest), metadata=md)
        print(f"{half}: wrote {dest} ({dest.stat().st_size / 1e9:.2f} GB)  check: {rep}", flush=True)
    fdir = WANGP / "finetunes"
    fdir.mkdir(exist_ok=True)
    entry = {
        "model": {
            "name": title,
            "architecture": "yue2",
            "description": f"{title}: a whole-model finetune of YuE2-3B, converted for Ten Forward from {Path(src_path).name}.",
            "URLs": [ac_name],
            "text_encoder_URLs": [f"YuE2_AR/{ar_name}"],
        }
    }
    (fdir / f"yue2_{short}.json").write_text(json.dumps(entry, indent=4), encoding="utf-8")
    print(f"finetune: {fdir / f'yue2_{short}.json'}  (Settings > Music model: {short})", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
