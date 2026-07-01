"""验证 parse_pptx 完整流程：python-pptx 提取 + OCR 补充 + 去重 + generate_script"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# 跳过 pix2tex（模型还在下载），只用 RapidOCR 文字识别
import core
core._pix2tex_model = False

from core import parse_pptx, generate_script

pptx = r"C:\Users\sun\Desktop\yuantu-test\digital-human\keling-batch\uploads\a5d5fe1e_5.2_.pptx"
print("=== parse_pptx 开始 ===")
scenes = parse_pptx(pptx)
print(f"=== 解析完成，共 {len(scenes)} 页 ===\n")

for sc in scenes:
    print(f"--- 第 {sc.index + 1} 页 ---")
    print(f"title: {sc.title!r}")
    print(f"bullets ({len(sc.bullets)} 条):")
    for b in sc.bullets:
        tag = "[公式]" if b.startswith("@@FORMULA@@") else "[文字]"
        print(f"  {tag} {b!r}")
    script = generate_script(sc)
    print(f"口播文案: {script!r}")
    print()
