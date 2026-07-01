"""调试 RapidOCR 原始返回 + 去重效果"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

import core
core._pix2tex_model = False

from rapidocr_onnxruntime import RapidOCR
ocr = RapidOCR()

img = r"C:\Users\sun\Desktop\yuantu-test\digital-human\keling-batch\uploads\_slides_a5d5fe1e_5.2_\slide_8.png"
raw, elapsed = ocr(img)
print(f"RapidOCR 原始返回 {len(raw) if raw else 0} 个块")
if raw:
    for i, item in enumerate(raw[:6]):
        box, text, conf = item[0], item[1], item[2]
        ys = [p[1] for p in box]
        print(f"  [{i}] y_center={sum(ys)/len(ys):.2f} y_bucket={int(sum(ys)/len(ys)//20)} text={text!r}")

# 现在调 _ocr_slide_image 看去重后结果
from core import _ocr_slide_image
items = _ocr_slide_image(img)
print(f"\n_ocr_slide_image 返回 {len(items)} 个块（应已去重）")
for i, (text, kind, y) in enumerate(items[:10]):
    print(f"  [{i}] y={y:.0f} kind={kind} text={text!r}")
