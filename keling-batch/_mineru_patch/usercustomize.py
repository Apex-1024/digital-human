"""usercustomize.py — 由 Python site 模块在启动时自动 import。

此文件通过 PYTHONPATH 注入到 MinerU LocalAPIServer 子进程，解决
pypdfium2 5.x 移除 PdfImage.get_pos() 导致 mineru.utils.pdf_classify
抛 AttributeError 的问题。

幂等：若 get_pos 已存在或 get_bounds 不可用则跳过。
"""
try:
    from pypdfium2._helpers.pageobjects import PdfImage
    if not hasattr(PdfImage, "get_pos") and hasattr(PdfImage, "get_bounds"):
        PdfImage.get_pos = lambda self: self.get_bounds()
except Exception:
    pass
