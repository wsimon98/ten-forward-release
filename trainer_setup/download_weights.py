"""One-shot: fetch every weight the trainer needs, then it runs with HF_HUB_OFFLINE=1.
Run with any python that has huggingface_hub:  python download_weights.py
Layout: MODELS_PATH=trainer/models (ComfyUI folder layout), HF_HOME=trainer/hf (MERT, SheetSage2 code)."""
import os, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
os.environ["HF_HOME"] = str(ROOT / "hf")
os.environ.pop("HF_HUB_OFFLINE", None)
from huggingface_hub import hf_hub_download, snapshot_download

MODELS = ROOT / "models"
for rel in ("checkpoints/yue2_3b_int8_convrot.safetensors", "audio_encoders/sheetsage2_bf16.safetensors"):
    print("get", rel, flush=True)
    hf_hub_download("Comfy-Org/YuE2", rel, local_dir=str(MODELS))
print("get MERT", flush=True)
snapshot_download("m-a-p/MERT-v2-FullSong")
print("get SheetSage2 code", flush=True)
snapshot_download("m-a-p/SheetSage2", allow_patterns=["*.py", "*.json", "requirements.txt"], ignore_patterns=["benchmarks/*", "tests/*", "render_assets/*"])
# the heads (v9 may already be in loras_incoming: reuse it)
tk = MODELS / "ai_toolkit"
v9_in = ROOT.parent / "loras_incoming" / "Mothersuperior__yue2-mothersuperior-realaudio-tokenizer-v4" / "tokenizer_head_joint_v9.bf16.safetensors"
v9 = tk / "tokenizer_head_joint_v9.bf16.safetensors"
if not v9.exists():
    if v9_in.exists():
        shutil.copy2(v9_in, v9)
    else:
        hf_hub_download("Mothersuperior/yue2-mothersuperior-realaudio-tokenizer-v4", "tokenizer_head_joint_v9.bf16.safetensors", local_dir=str(tk))
v4 = tk / "tokenizer_head_joint_v4.bf16.safetensors"
if not v4.exists():
    hf_hub_download("Mothersuperior/yue2-mothersuperior-realaudio-tokenizer-v4", "tokenizer_head_joint_v4.bf16.safetensors", local_dir=str(tk))
print("get melbandroformer (only for the Advanced 'split vocals' option)", flush=True)
hf_hub_download("ai-toolkit/melbandroformer", "melbandroformer_vocals_kj.safetensors", local_dir=str(MODELS / "checkpoints"))
print("ALL_DONE", flush=True)
