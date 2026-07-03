"""直接调用 parse_pptx 输出识别的所有文案"""
import os
import sys
import json
import io

# 重定向 stdout 到文件，避免 PowerShell 混乱
_out_path = os.path.join(os.path.dirname(__file__), "_parse_output.txt")
_out = open(_out_path, "w", encoding="utf-8")
_old_stdout = sys.stdout
sys.stdout = _out

_model_root = os.path.join(os.path.dirname(__file__), ".p2t_models")
os.environ.setdefault("PIX2TEXT_HOME", _model_root)
os.environ.setdefault("CNSTD_HOME", _model_root)
os.environ.setdefault("CNOCR_HOME", _model_root)
os.environ.setdefault("YOLO_CONFIG_DIR", _model_root)
os.environ.setdefault("ULTRALYTICS_CONFIG_DIR", _model_root)
os.environ.setdefault("HF_HOME", _model_root)
os.environ.setdefault("XDG_CACHE_HOME", _model_root)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
from core import parse_pptx, generate_script

pptx_path = os.path.join(os.path.dirname(__file__), "uploads", "test_clean.pptx")
print(f"解析: {pptx_path}\n{'='*60}")

scenes = parse_pptx(pptx_path)
for s in scenes:
    print(f"\n第 {s.index + 1} 页 | 标题: {s.title}")
    print(f"  图片: {s.image_path}")
    print(f"  bullets ({len(s.bullets)} 条):")
    for i, b in enumerate(s.bullets):
        print(f"    [{i}] {b}")

print(f"\n{'='*60}")
print("生成口播文案:\n")

for s in scenes:
    script = generate_script(s)
    print(f"第 {s.index + 1} 页: {script}")
    print()

sys.stdout = _old_stdout
_out.close()
print(f"输出已写入 {_out_path}")

