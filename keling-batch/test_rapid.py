"""只测 RapidOCR 文字识别（跳过 pix2tex 公式重识别，避免等模型下载）"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# 在导入 core 前先标记 pix2tex 不可用，跳过模型下载
import core
core._pix2tex_model = False

from core import _ocr_slide_image, generate_script, Scene, latex_to_chinese

# 之前 jobs.json 里 slide_2 / slide_3 / slide_5 / slide_8 是 bullets 全空的纯公式页
for idx in [0, 1, 2, 3, 4, 5, 6, 7, 8]:
    img = rf"C:\Users\sun\Desktop\yuantu-test\digital-human\keling-batch\uploads\_slides_a5d5fe1e_5.2_\slide_{idx}.png"
    print(f"\n=== slide_{idx} ===")
    items = _ocr_slide_image(img)
    print(f"识别到 {len(items)} 个块:")
    for i, (text, kind, y) in enumerate(items):
        print(f"  [{i}] y={y:.0f} kind={kind} text={text!r}")

    # 模拟 generate_script：用 OCR 结果构造 Scene 看口播文案
    bullets = []
    for text, kind, _y in items:
        if kind == "formula":
            bullets.append(f"@@FORMULA@@{text}")
        else:
            bullets.append(text)
    # 取第一个文字作为标题（粗略模拟）
    title = items[0][0] if items and items[0][1] == "text" else f"第 {idx+1} 页"
    scene = Scene(index=idx, title=title, bullets=bullets)
    script = generate_script(scene)
    print(f"  → 口播文案: {script!r}")
