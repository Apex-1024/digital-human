"""单页 OCR 验证：PaddleOCR 文字 + pix2tex 公式，并展示 latex_to_chinese 转换"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from core import _ocr_slide_image, latex_to_chinese

# 选 slide_2.png（之前 jobs.json 里这一页 bullets 全空，是纯公式页）
img = r"C:\Users\sun\Desktop\yuantu-test\digital-human\keling-batch\uploads\_slides_a5d5fe1e_5.2_\slide_2.png"
print(f"=== OCR {img} ===")
items = _ocr_slide_image(img)
print(f"\n识别到 {len(items)} 个块:")
for i, (text, kind, y) in enumerate(items):
    print(f"  [{i}] y={y:.0f} kind={kind}")
    print(f"      raw: {text!r}")
    if kind == "formula":
        spoken = latex_to_chinese(text)
        print(f"      中文读法: {spoken!r}")

print("\n=== latex_to_chinese 单元测试 ===")
cases = [
    r"\int_a^b f(x) dx",
    r"\frac{a}{b}",
    r"\sqrt{x+1}",
    r"x^2 + y^2 = r^2",
    r"\sum_{i=1}^{n} a_i",
    r"\lim_{x \to 0} \frac{\sin x}{x}",
    r"\Phi(x) = \int_a^x f(t) dt",
    r"\alpha + \beta = \gamma",
]
for c in cases:
    print(f"  {c}")
    print(f"  -> {latex_to_chinese(c)!r}")
    print()
