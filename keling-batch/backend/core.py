"""
课灵 AI 批量制课系统 — 后端核心
功能：
  1. 解析 PPT 文件 → 拆解为分镜（每页 title + bullets + 配图）
  2. 为每页生成讲解稿（基于页内文字的启发式 + 可选 LLM 增强）
  3. TTS 合成语音（edge-tts，免费中文音色）
  4. 用 FFmpeg 合成"PPT 页面 + 数字人画面 + 语音"成片
  5. 后台任务队列，支持批量并发
"""
import os
import re
import sys
import json
import time
import uuid
import shutil
import threading
import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 路径配置 ============
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
ASSETS_DIR = BASE_DIR / "assets"
AVATAR_DIR = ASSETS_DIR / "avatars"
BGM_DIR = ASSETS_DIR / "bgm"
JOBS_FILE = BASE_DIR / "jobs.json"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
BGM_DIR.mkdir(parents=True, exist_ok=True)

# ============ 日志 ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("keling-batch")

# ============ FFmpeg 路径 ============
# 优先用 imageio-ffmpeg 自带的完整版（带 lavfi/aac/mp3 编码器）
# 回退到系统 PATH 中的 ffmpeg
def _find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(path).exists():
            log.info(f"使用 ffmpeg: {path}")
            return path
    except ImportError:
        pass
    return "ffmpeg"  # 退到 PATH


FFMPEG_BIN = _find_ffmpeg()
FFPROBE_BIN = FFMPEG_BIN  # 同包中也有 ffprobe

# ============ OCR 懒加载（RapidOCR / PaddleOCR + pix2tex） ============
# MathType 公式在 PPT 里是 OLE 嵌入对象，python-pptx 读不到，
# 必须靠 OCR 把渲染好的 slide_N.png 里的公式和文字补回来。
_ocr_engine = None       # RapidOCR 或 PaddleOCR 实例
_ocr_engine_kind = None  # "rapid" | "paddle" | None
_pix2tex_model = None


def _get_ocr_engine():
    """
    懒加载 OCR 引擎。
      1. 优先 RapidOCR（onnxruntime 后端，无 paddle 兼容性问题，稳定）
      2. 回退 PaddleOCR（paddle 后端，2.x/3.x 兼容）
    返回 (engine, kind) 或 (None, None)
    """
    global _ocr_engine, _ocr_engine_kind
    if _ocr_engine is not None:
        return _ocr_engine, _ocr_engine_kind
    if _ocr_engine is False:
        return None, None

    # ---- 1. 优先 RapidOCR ----
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
        _ocr_engine_kind = "rapid"
        log.info("RapidOCR (onnxruntime) 初始化成功")
        return _ocr_engine, _ocr_engine_kind
    except Exception as e:
        log.debug(f"RapidOCR 不可用：{e}")

    # ---- 2. 回退 PaddleOCR ----
    try:
        from paddleocr import PaddleOCR
        # 兼容 2.x / 3.x API：3.x 不接受 use_angle_cls/show_log 等旧参数
        last_err = None
        candidates = (
            {"lang": "ch",
             "use_doc_orientation_classify": False,
             "use_doc_unwarping": False},                # 3.x 精简
            {"lang": "ch", "use_angle_cls": True},       # 2.x
            {"lang": "ch"},                              # 最小化兜底
        )
        for kwargs in candidates:
            try:
                _ocr_engine = PaddleOCR(**kwargs)
                _ocr_engine_kind = "paddle"
                log.info(f"PaddleOCR 初始化成功 (kwargs={kwargs})")
                return _ocr_engine, _ocr_engine_kind
            except Exception as e:
                last_err = e
                continue
        log.warning(f"PaddleOCR 所有参数组合都失败：{last_err}")
    except Exception as e:
        log.warning(f"PaddleOCR 导入失败：{e}")

    _ocr_engine = False
    _ocr_engine_kind = None
    return None, None


def _get_pix2tex():
    """懒加载 pix2tex 公式识别模型"""
    global _pix2tex_model
    if _pix2tex_model is not None:
        return _pix2tex_model
    try:
        from pix2tex.cli import LatexOCR
        _pix2tex_model = LatexOCR()
        log.info("pix2tex (LaTeXOCR) 初始化成功")
    except Exception as e:
        log.warning(f"pix2tex 初始化失败：{e}")
        _pix2tex_model = False
    return _pix2tex_model


def _bbox_center(box):
    """PaddleOCR 返回的 bbox 是 4 个点，取中心 y"""
    ys = [p[1] for p in box]
    return sum(ys) / len(ys)


def _bbox_area(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def _bbox_overlap(box_a, box_b):
    """两个 bbox 的相交面积（用于去重：PaddleOCR 文字 vs pix2tex 公式）"""
    ax1, ay1 = min(p[0] for p in box_a), min(p[1] for p in box_a)
    ax2, ay2 = max(p[0] for p in box_a), max(p[1] for p in box_a)
    bx1, by1 = min(p[0] for p in box_b), min(p[1] for p in box_b)
    bx2, by2 = max(p[0] for p in box_b), max(p[1] for p in box_b)
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    return ix * iy


# 触发数学符号判定（说明这一块很可能是公式，要交给 pix2tex 重识别）
_MATH_HINT_CHARS = set("=±÷×∑∫√π∞αβγδθλμνΦφΩω≤≥≠≈→←↔∈∉∀∃∝·²³")


def _looks_like_formula(text: str) -> bool:
    if not text:
        return False
    # 含明确数学符号
    if any(c in _MATH_HINT_CHARS for c in text):
        return True
    # 含 LaTeX 风格的转义
    if "\\" in text or "^" in text or "_" in text:
        return True
    # 单字母+等号 数字密集（如 a=2, x=1）
    digits = sum(1 for c in text if c.isdigit())
    if "=" in text and digits >= 1:
        return True
    return False


def _crop_bbox(img, box):
    """从 PIL Image 上按 PaddleOCR bbox 裁剪区域"""
    xs = [int(p[0]) for p in box]
    ys = [int(p[1]) for p in box]
    # 留一点边距，pix2tex 识别更稳
    pad = 4
    left = max(0, min(xs) - pad)
    upper = max(0, min(ys) - pad)
    right = min(img.width, max(xs) + pad)
    lower = min(img.height, max(ys) + pad)
    if right - left < 4 or lower - upper < 4:
        return None
    return img.crop((left, upper, right, lower))


def _ocr_slide_image(image_path: str) -> List[tuple]:
    """
    对单页 PPT 渲染图做 OCR，返回 [(text, kind, y), ...]
      kind = "text"   普通文字
      kind = "formula" 数学公式（latex 原文）
    按 y 中心升序排列（与阅读顺序一致）。
    """
    result: List[tuple] = []
    ocr, kind = _get_ocr_engine()
    if not ocr:
        log.warning(f"OCR 跳过（无可用引擎）：{image_path}")
        return result

    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        log.warning(f"打开图片失败 {image_path}：{e}")
        return result

    # ---- 1. 调用 OCR 引擎，归一化为 list of (box, text, conf) ----
    flat = []
    try:
        if kind == "rapid":
            # RapidOCR 返回 (list_or_None, elapsed)
            # 每个 item: [box, text, conf]
            raw, _elapsed = ocr(image_path)
            if raw:
                for item in raw:
                    box, text, conf = item[0], item[1], float(item[2])
                    flat.append((box, text, conf))
        elif kind == "paddle":
            # PaddleOCR 2.x: ocr.ocr(path, cls=True) → [[ [box,(text,conf)], ... ]]
            # PaddleOCR 3.x: ocr.predict(path) → dict-like
            try:
                paddle_raw = ocr.ocr(image_path, cls=True)
            except TypeError:
                paddle_raw = ocr.predict(image_path)
            except Exception as e:
                log.warning(f"PaddleOCR 调用失败：{e}")
                paddle_raw = None
            if paddle_raw:
                pages = paddle_raw if isinstance(paddle_raw, list) else [paddle_raw]
                for page in pages:
                    if not page:
                        continue
                    if isinstance(page, dict) and "rec_texts" in page:
                        texts = page.get("rec_texts", [])
                        scores = page.get("rec_scores", [1.0] * len(texts))
                        polys = page.get("rec_polys") or page.get("dt_polys", [])
                        for t, s, b in zip(texts, scores, polys):
                            flat.append((b, t, float(s) if s is not None else 0.0))
                    elif isinstance(page, list):
                        for item in page:
                            try:
                                box, (text, conf) = item
                                flat.append((box, text, float(conf)))
                            except (ValueError, TypeError):
                                continue
    except Exception as e:
        log.warning(f"OCR 调用失败：{e}")
        return result

    if not flat:
        return result

    # ---- 1.5 去重：相同 text 且 bbox 中心 y 相近（<20px）的视为重复 ----
    # RapidOCR 偶发会返回同一行多次（多候选/角度分类叠加），y 有 1-2px 微小差异
    deduped = []
    seen_keys = set()
    for box, text, conf in flat:
        text_s = text.strip() if text else ""
        if not text_s:
            continue
        # 20px 量化桶，容纳 RapidOCR 的 y 坐标微小抖动
        key = (text_s, int(_bbox_center(box) // 20))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((box, text, conf))
    flat = deduped

    # ---- 2. 遍历所有块：疑似公式交给 pix2tex，普通文字直接保留 ----
    # 单循环避免重复加入（旧版本第 2/3 步分开导致同一项被加两次）
    pix = _get_pix2tex()
    for box, text, conf in flat:
        if not text or not text.strip():
            continue
        # 疑似公式（含数学符号 / 低置信度）→ crop 后交给 pix2tex 重识别为 LaTeX
        if _looks_like_formula(text) or conf < 0.6:
            if pix:
                crop = _crop_bbox(img, box)
                if crop:
                    try:
                        latex = pix(crop)
                        if latex:
                            latex = latex.strip()
                            # 质量检查：pix2tex 对复杂多行公式容易产生垃圾 LaTeX
                            # 特征：输出过长（>80字符）、反斜杠过多（>8个）
                            if latex and len(latex) <= 80 and latex.count("\\") <= 8:
                                result.append((latex, "formula", _bbox_center(box)))
                                continue
                            elif latex:
                                log.debug(f"pix2tex 输出疑似垃圾（len={len(latex)} backslashes={latex.count(chr(92))}），回退到 OCR 文本")
                    except Exception as e:
                        log.debug(f"pix2tex 识别失败：{e}")
            # pix2tex 不可用、识别失败或输出垃圾 → 退回 OCR 文本（公式区域也保留下来）
            result.append((text.strip(), "text", _bbox_center(box)))
        else:
            # 普通文字
            result.append((text.strip(), "text", _bbox_center(box)))

    # 按 y 升序，模拟阅读顺序
    result.sort(key=lambda x: x[2])
    return result


def latex_to_chinese(latex: str) -> str:
    """
    把常见 LaTeX 公式转成中文口播读法（用于 TTS）。
    覆盖：积分、求和、极限、分数、根号、上下标、希腊字母、关系符。
    不追求严格等价，追求念出来顺口。
    """
    if not latex:
        return ""
    s = latex.strip()
    # 去掉 $...$ 包裹
    s = s.strip("$").strip()

    # 希腊字母
    greek = {
        r"\alpha": "阿尔法", r"\beta": "贝塔", r"\gamma": "伽马",
        r"\delta": "德尔塔", r"\epsilon": "艾普西隆", r"\varepsilon": "艾普西隆",
        r"\zeta": "泽塔", r"\eta": "伊塔", r"\theta": "西塔", r"\vartheta": "西塔",
        r"\iota": "约塔", r"\kappa": "卡帕", r"\lambda": "拉姆达",
        r"\mu": "缪", r"\nu": "纽", r"\xi": "克西",
        r"\pi": "派", r"\varpi": "派", r"\rho": "柔", r"\varrho": "柔",
        r"\sigma": "西格玛", r"\varsigma": "西格玛",
        r"\tau": "陶", r"\upsilon": "宇普西隆", r"\phi": "弗爱", r"\varphi": "弗爱",
        r"\chi": "凯", r"\psi": "普西", r"\omega": "欧米伽",
        r"\Gamma": "伽马", r"\Delta": "德尔塔", r"\Theta": "西塔",
        r"\Lambda": "拉姆达", r"\Xi": "克西", r"\Pi": "派",
        r"\Sigma": "西格玛", r"\Phi": "弗爱", r"\Psi": "普西", r"\Omega": "欧米伽",
    }
    for k, v in greek.items():
        s = s.replace(k, v)

    # 极限：\lim_{x \to a} → x 趋近于 a 时的极限
    s = re.sub(r"\\lim_\{?([^}\\]+)\s*\\to\s*([^}\\]+)\}?",
               r"极限 \1 趋近于 \2 ", s)
    s = s.replace(r"\lim", "极限 ")
    s = s.replace(r"\to", " 趋近于 ")

    # 积分：\int_a^b 或 \int_{a}^{b} → 从 a 到 b 积分
    # 注意：下标/上标内容里不能再吃掉 ^ 和 _（否则 _a^b 会把 a^b 当成下标）
    m = re.search(r"\\int(?:_\{?([^{_}^]+)\}?)?(?:\^\{?([^{_}^]+)\}?)?", s)
    if m:
        lo, hi = m.group(1), m.group(2)
        repl = "积分"
        if lo and hi:
            repl = f"从 {lo.strip()} 到 {hi.strip()} 的积分"
        elif lo:
            repl = f"下限 {lo.strip()} 的积分"
        elif hi:
            repl = f"上限 {hi.strip()} 的积分"
        s = s.replace(m.group(0), repl, 1)
    s = s.replace(r"\int", "积分")
    s = s.replace(r"\,dx", " 微元 d x").replace(r"\,dt", " 微元 d t")
    s = s.replace(r"\,dy", " 微元 d y").replace(" dx", " 微元 d x").replace(" dt", " 微元 d t")

    # 求和：\sum_{i=1}^{n} → i 从 1 到 n 求和
    m = re.search(r"\\sum_\{?([^{_}^]+)\}?(?:\^\{?([^{_}^]+)\}?)?", s)
    if m:
        lo, hi = m.group(1), m.group(2)
        repl = "求和"
        if lo and hi:
            repl = f"{lo.strip()} 到 {hi.strip()} 求和"
        s = s.replace(m.group(0), repl, 1)
    s = s.replace(r"\sum", "求和")

    # 乘积：\prod
    s = s.replace(r"\prod", "求积")

    # 分数：\frac{a}{b} → b 分之 a
    def _frac(m):
        a, b = m.group(1), m.group(2)
        return f" {b} 分之 {a} "
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", _frac, s)
    s = re.sub(r"\\dfrac\{([^{}]*)\}\{([^{}]*)\}", _frac, s)
    s = re.sub(r"\\tfrac\{([^{}]*)\}\{([^{}]*)\}", _frac, s)

    # 二项式系数
    def _binom(m):
        a, b = m.group(1), m.group(2)
        return f" 从 {b} 中取 {a} "
    s = re.sub(r"\\binom\{([^{}]*)\}\{([^{}]*)\}", _binom, s)

    # 根号：\sqrt{x} / \sqrt[n]{x}
    m = re.search(r"\\sqrt\[(.*?)\]\{(.*?)\}", s)
    if m:
        s = s.replace(m.group(0), f" {m.group(1)} 次根号下 {m.group(2)} ", 1)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r" 根号下 \1 ", s)
    s = s.replace(r"\sqrt", "根号")

    # 上下标：a^{bc} → a 的 bc 次方；a_{ij} → a 下标 ij
    s = re.sub(r"\^\{([^{}]+)\}", r" 的 \1 次方", s)
    s = re.sub(r"_\{([^{}]+)\}", r" 下标 \1", s)
    s = re.sub(r"\^([0-9A-Za-z])", r" 的 \1 次方", s)
    s = re.sub(r"_([0-9A-Za-z])", r" 下标 \1", s)

    # 关系符
    rel = {
        r"\leq": " 小于等于 ", r"\le": " 小于等于 ",
        r"\geq": " 大于等于 ", r"\ge": " 大于等于 ",
        r"\neq": " 不等于 ", r"\ne": " 不等于 ",
        r"\approx": " 约等于 ", r"\equiv": " 恒等于 ",
        r"\in": " 属于 ", r"\notin": " 不属于 ",
        r"\subset": " 包含于 ", r"\subseteq": " 包含于 ",
        r"\cup": " 并集 ", r"\cap": " 交集 ",
        r"\forall": " 任意 ", r"\exists": " 存在 ",
        r"\infty": " 无穷 ", r"\partial": " 偏导 ",
        r"\nabla": " 梯度 ", r"\pm": " 正负 ", r"\mp": " 负正 ",
        r"\times": " 乘以 ", r"\div": " 除以 ", r"\cdot": " 乘 ",
        r"\rightarrow": " 趋向 ", r"\to": " 趋向 ",
        r"\Rightarrow": " 推出 ", r"\Leftrightarrow": " 等价于 ",
        r"\langle": " 左尖 ", r"\rangle": " 右尖 ",
        r"\|": " 平行 ", r"\perp": " 垂直 ",
    }
    for k, v in rel.items():
        s = s.replace(k, v)

    # 算符
    s = s.replace(r"\log", " 对数 ").replace(r"\ln", " 自然对数 ")
    s = s.replace(r"\sin", " 正弦 ").replace(r"\cos", " 余弦 ")
    s = s.replace(r"\tan", " 正切 ").replace(r"\cot", " 余切 ")
    s = s.replace(r"\exp", " 指数 ").replace(r"\max", " 最大值 ")
    s = s.replace(r"\min", " 最小值 ").replace(r"\det", " 行列式 ")

    # 字体修饰去掉
    s = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathbb\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathbf\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\left|", " ", s)
    s = re.sub(r"\\right|", " ", s)
    s = s.replace(r"\left", " ").replace(r"\right", " ")
    s = s.replace(r"\big", " ").replace(r"\Big", " ")
    s = s.replace(r"\,", " ").replace(r"\;", " ").replace(r"\:", " ")
    s = s.replace(r"\!", "")
    s = s.replace(r"\quad", " ").replace(r"\qquad", " ")
    s = s.replace(r"\,", " ")

    # 残留反斜杠命令直接丢掉
    s = re.sub(r"\\[a-zA-Z]+", " ", s)

    # 多余符号
    s = s.replace("&", " 且 ").replace("\\", " ")
    s = s.replace("^", " 的次方 ").replace("_", " 下标 ")
    s = s.replace("{", " ").replace("}", " ")
    s = s.replace("$", " ")

    # 折叠多空格
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ============ 任务状态 ============
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELED = "canceled"


@dataclass
class Scene:
    index: int
    title: str
    bullets: List[str] = field(default_factory=list)
    image_path: Optional[str] = None
    script: str = ""
    audio_path: Optional[str] = None
    duration_sec: float = 0.0


@dataclass
class Job:
    job_id: str
    filename: str
    pptx_path: str
    status: str = JOB_STATUS_PENDING
    progress: float = 0.0
    stage: str = "等待中"
    avatar: str = "teacher_female"
    voice: str = "zh-CN-XiaoxiaoNeural"
    ratio: str = "16:9"
    resolution: str = "720p"
    digital_human_mode: str = "auto"  # auto=sadtalker优先 | static=静态头像
    enable_subtitle: bool = True
    enable_bgm: bool = True
    scenes: List[Scene] = field(default_factory=list)
    output_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class JobStore:
    """简单的 JSON 文件持久化的任务存储"""
    def __init__(self, path: Path = JOBS_FILE):
        self.path = path
        self.lock = threading.Lock()
        self._cache: Dict[str, Job] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                for j in data.get("jobs", []):
                    # 重建 Scene 对象
                    scenes = [Scene(**s) for s in j.pop("scenes", [])]
                    self._cache[j["job_id"]] = Job(scenes=scenes, **j)
            except Exception as e:
                log.warning(f"加载 jobs.json 失败：{e}")

    def _persist(self):
        with self.path.open("w", encoding="utf-8") as f:
            data = {"jobs": [j.to_dict() for j in self._cache.values()]}
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add(self, job: Job):
        with self.lock:
            self._cache[job.job_id] = job
            self._persist()

    def update(self, job_id: str, **kwargs):
        with self.lock:
            job = self._cache.get(job_id)
            if not job:
                return None
            for k, v in kwargs.items():
                setattr(job, k, v)
            self._persist()
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self._cache.get(job_id)

    def all(self) -> List[Job]:
        with self.lock:
            return sorted(self._cache.values(), key=lambda j: j.created_at, reverse=True)

    def delete(self, job_id: str):
        with self.lock:
            self._cache.pop(job_id, None)
            self._persist()


store = JobStore()


# ============ 1. PPT 解析 ============
def parse_pptx(pptx_path: str) -> List[Scene]:
    """
    使用 python-pptx 解析每页幻灯片，提取：
      - 标题（最大的字号 or 第一行）
      - 要点（其余文本）
      - 配图（保存为临时 PNG，用 LibreOffice 渲染）
    返回 Scene 列表
    """
    try:
        from pptx import Presentation
    except ImportError:
        raise RuntimeError("缺少 python-pptx，请先运行 pip install python-pptx")

    prs = Presentation(pptx_path)
    scenes: List[Scene] = []

    # 用 libreoffice 把每一页转成 PNG
    pptx_path = Path(pptx_path)
    slide_imgs_dir = pptx_path.parent / f"_slides_{pptx_path.stem}"
    slide_imgs_dir.mkdir(exist_ok=True)
    _convert_pptx_to_images(pptx_path, slide_imgs_dir)

    img_files = sorted(slide_imgs_dir.glob("slide_*.png"))

    for idx, slide in enumerate(prs.slides):
        title = ""
        bullets: List[str] = []
        seen_text: List[str] = []  # 用于 OCR 去重
        # 取所有文本框
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            tf = shape.text_frame
            for para in tf.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if not text:
                    continue
                # 标题判定：第一个非空段落 or 字号最大
                if not title:
                    title = text
                else:
                    bullets.append(text)
                seen_text.append(text)

        img_path = str(img_files[idx]) if idx < len(img_files) else None

        # ========== OCR 补充 ==========
        # MathType/OLE 公式 python-pptx 读不到，整页 OCR 把公式和漏掉的文字补回来。
        # 公式用 @@FORMULA@@<latex> 前缀标记，generate_script 里转成中文口播读法。
        if img_path and Path(img_path).exists():
            try:
                ocr_items = _ocr_slide_image(img_path)
                for text, kind, _y in ocr_items:
                    if kind == "formula":
                        bullets.append(f"@@FORMULA@@{text}")
                    else:
                        # 模糊去重：OCR 文字若已在 python-pptx 提取过则跳过
                        norm = text.replace(" ", "").replace(":", "：")
                        if any(norm and (norm in s.replace(" ", "") or
                                         s.replace(" ", "") in norm)
                               for s in seen_text if s):
                            continue
                        bullets.append(text)
                        seen_text.append(text)
            except Exception as e:
                log.warning(f"第 {idx + 1} 页 OCR 失败：{e}")

        # 兜底标题
        if not title:
            title = f"第 {idx + 1} 页"

        scenes.append(Scene(index=idx, title=title, bullets=bullets, image_path=img_path))

    return scenes


def _ppt_com_to_pdf(pptx_path: Path, pdf_path: Path) -> bool:
    """用 PowerPoint COM 自动化把 PPT 转 PDF（需要 Office + pywin32）"""
    try:
        import win32com.client
        import pythoncom
    except ImportError:
        log.info("pywin32 未安装，跳过 PowerPoint COM 方案")
        return False

    abs_pptx = str(pptx_path.resolve())
    abs_pdf = str(pdf_path.resolve())
    app = None
    pres = None
    try:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("PowerPoint.Application")
        # WithWindow=False 不显示 PowerPoint 窗口
        pres = app.Presentations.Open(abs_pptx, ReadOnly=True, WithWindow=False)
        # 32 = ppSaveAsPDF
        pres.SaveAs(abs_pdf, 32)
        pres.Close()
        pres = None
        app.Quit()
        app = None
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            log.info(f"PowerPoint COM: {pdf_path.stat().st_size} bytes")
            return True
        return False
    except Exception as e:
        log.warning(f"PowerPoint COM 转换失败：{e}")
        return False
    finally:
        try:
            if pres:
                pres.Close()
        except Exception:
            pass
        try:
            if app:
                app.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _soffice_to_pdf(pptx_path: Path, pdf_path: Path, timeout: int = 180) -> bool:
    """用 LibreOffice 转 PDF，Popen + 轮询文件，避免 Windows timeout 不生效"""
    import tempfile
    tmp_profile = tempfile.mkdtemp(prefix="lo_profile_")
    profile_url = Path(tmp_profile).as_uri()

    cmd = [
        "soffice",
        f"-env:UserInstallation={profile_url}",
        "--headless", "--convert-to", "pdf",
        "--outdir", str(pdf_path.parent), str(pptx_path)
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        log.warning("未找到 soffice (LibreOffice)")
        return False

    start = time.time()
    while True:
        elapsed = time.time() - start
        # 检查进程是否已退出
        if proc.poll() is not None:
            # 进程退出，检查 PDF
            break
        # 检查 PDF 是否已生成且大小稳定
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            # 等待 1 秒再检查一次，确保文件完全写入
            time.sleep(1)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                shutil.rmtree(tmp_profile, ignore_errors=True)
                log.info(f"LibreOffice: PDF 生成成功（{elapsed:.0f}s）")
                return True
        # 超时
        if elapsed > timeout:
            log.warning(f"LibreOffice 转换超时（{timeout}s），终止进程")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            shutil.rmtree(tmp_profile, ignore_errors=True)
            # 即使超时，PDF 可能已经生成
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                log.info("LibreOffice: 超时但 PDF 已生成，继续使用")
                return True
            return False
        time.sleep(2)

    # 进程已退出
    shutil.rmtree(tmp_profile, ignore_errors=True)
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        log.info(f"LibreOffice: PDF 生成成功（{time.time()-start:.0f}s）")
        return True
    log.warning(f"LibreOffice: 进程退出但无 PDF（exit={proc.returncode}）")
    return False


def _convert_pptx_to_images(pptx_path: Path, out_dir: Path):
    """
    把 PPT 的每一页原样转成 PNG（保留原版版式）。
    链路（三选一）：
      A. PowerPoint COM → PDF → pypdfium2 逐页 PNG（最快，需 Office + pywin32）
      B. LibreOffice → PDF → pypdfium2 逐页 PNG（较慢，但免费）
      C. python-pptx + Pillow 简化渲染（兜底）
    """
    pdf_path = out_dir.parent / (pptx_path.stem + ".pdf")

    # 0) 先杀掉可能残留的 soffice 进程
    try:
        subprocess.run(["taskkill", "/F", "/IM", "soffice.exe"],
                       capture_output=True, timeout=5)
        subprocess.run(["taskkill", "/F", "/IM", "soffice.bin"],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    # A) PowerPoint COM 转 PDF（最快）
    if _ppt_com_to_pdf(pptx_path, pdf_path):
        log.info(f"PowerPoint COM 转 PDF 成功：{pdf_path}")
        _pdf_to_images(pdf_path, out_dir)
        if any(out_dir.glob("slide_*.png")):
            return

    # B) LibreOffice 转 PDF（用 Popen + 轮询，避免 timeout 不生效）
    if _soffice_to_pdf(pptx_path, pdf_path, timeout=180):
        log.info(f"LibreOffice 转 PDF 成功：{pdf_path}")
        # 等待 1 秒确保 PDF 完全写入
        time.sleep(1)
        _pdf_to_images(pdf_path, out_dir)
        if any(out_dir.glob("slide_*.png")):
            return

    # C) 回退：python-pptx + Pillow 简化渲染
    log.warning("使用 Pillow 简化渲染（非原版样式）")
    _render_pptx_with_pillow(pptx_path, out_dir)


def _render_pptx_with_pillow(pptx_path: Path, out_dir: Path):
    """用 python-pptx 读取内容，用 Pillow 直接绘制每页"""
    from pptx import Presentation
    from PIL import Image, ImageDraw, ImageFont

    prs = Presentation(pptx_path)
    W, H = 1280, 720
    palette = [
        ("#1a1f3a", "#2a2f5c"),  # 深蓝
        ("#3a4ba0", "#6c8cff"),  # 蓝紫
        ("#5a2da0", "#b06bff"),  # 紫
        ("#1f8a7a", "#36d6c0"),  # 青
        ("#b0306b", "#ff6ba0"),  # 粉
    ]
    try:
        font_big = ImageFont.truetype("arial.ttf", 60)
        font_sm = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        font_big = ImageFont.load_default()
        font_sm = ImageFont.load_default()

    for idx, slide in enumerate(prs.slides):
        c1, c2 = palette[idx % len(palette)]
        img = Image.new("RGB", (W, H), c1)
        d = ImageDraw.Draw(img)
        # 渐变
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        for y in range(H):
            t = y / H
            r = int(r1 + (r2 - r1) * t); g = int(g1 + (g2 - g1) * t); b = int(b1 + (b2 - b1) * t)
            d.line([(0, y), (W, y)], fill=(r, g, b))

        # 收集文字
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        texts.append(t)
        title = texts[0] if texts else f"第 {idx + 1} 页"
        bullets = texts[1:6]

        # 绘制标题
        d.text((W // 2, 120), title, fill="white", anchor="mt", font=font_big)
        # 绘制要点
        for i, b in enumerate(bullets):
            y = 240 + i * 60
            d.text((100, y), f"• {b}", fill="white", font=font_sm)
        # 角标
        d.text((W - 40, H - 40), f"{idx + 1}", fill="#9aa1c7", anchor="rb", font=font_sm)
        img.save(out_dir / f"slide_{idx}.png", "PNG")
        log.info(f"渲染第 {idx + 1} 页 → slide_{idx}.png")


def _pdf_to_images(pdf_path: Path, out_dir: Path):
    """PDF 每页 → PNG。优先 pypdfium2（纯 wheel 无外部依赖），回退 pdf2image。"""
    # 1) pypdfium2
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf_path))
        for i in range(len(doc)):
            page = doc[i]
            bitmap = page.render(scale=2.0)  # scale=2 → 约 300dpi
            img = bitmap.to_pil()
            img.save(out_dir / f"slide_{i}.png", "PNG")
        log.info(f"pypdfium2: PDF → {len(doc)} 张 PNG")
        return
    except ImportError:
        log.warning("pypdfium2 未安装，尝试 pdf2image")
    except Exception as e:
        log.warning(f"pypdfium2 渲染失败：{e}")

    # 2) pdf2image（需要 poppler）
    try:
        from pdf2image import convert_from_path
        pages = convert_from_path(str(pdf_path), dpi=150)
        for i, page in enumerate(pages):
            page.save(out_dir / f"slide_{i}.png", "PNG")
        log.info(f"pdf2image: PDF → {len(pages)} 张 PNG")
    except Exception as e:
        log.warning(f"pdf 转图片失败：{e}")


# ============ 2. 讲解稿生成 ============
def generate_script(scene: Scene) -> str:
    """
    把每页的 title + bullets 拼成口播文案。
      - @@FORMULA@@<latex> 前缀的 bullet 视为公式，转成中文口播读法
      - 严格只用本页内容，不跨页拼接；超长按当前页截断（不挪到下一页）
    实际生产可对接 LLM（豆包 / 通义 / OpenAI）做润色。
    """
    parts = [scene.title + "。"]
    for b in scene.bullets:
        if not b:
            continue
        if b.startswith("@@FORMULA@@"):
            latex = b[len("@@FORMULA@@"):]
            spoken = latex_to_chinese(latex)
            if spoken:
                parts.append(spoken + "。")
        else:
            b = b.strip().rstrip("。.")
            if b:
                parts.append(b + "。")
    text = " ".join(parts)
    # 限制长度（避免 TTS 过长）；当前页超长直接截断，不挪到下一页
    if len(text) > 400:
        text = text[:400] + "..."
    return text


# ============ 3. TTS 合成 ============
def tts_synthesize(text: str, voice: str, out_path: str) -> float:
    """
    用 edge-tts 合成中文语音，返回音频时长（秒）。
    失败时回退到 pyttsx3（离线，但效果差）。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 优先 edge-tts
    try:
        import edge_tts
        import asyncio

        async def _run():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(out_path))

        asyncio.run(_run())
        if out_path.exists() and out_path.stat().st_size > 0:
            return _audio_duration(out_path)
    except Exception as e:
        log.warning(f"edge-tts 失败：{e}")

    # 回退：pyttsx3（离线）
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        if out_path.exists() and out_path.stat().st_size > 0:
            return _audio_duration(out_path)
    except Exception as e:
        log.warning(f"pyttsx3 失败：{e}")

    # 兜底：生成静音音频
    log.warning("TTS 全部失败，生成静音占位")
    _make_silent_audio(out_path, duration=3.0)
    return 3.0


def _audio_duration(audio_path: Path) -> float:
    """用 ffmpeg（非 ffprobe）探测音频时长，兼容 imageio-ffmpeg 只带 ffmpeg 的情况"""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", str(audio_path),
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=10
        )
        # ffmpeg 把时长信息输出在 stderr，格式: Duration: 00:00:03.12
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 3.0


def _make_silent_audio(out_path: Path, duration: float):
    """生成指定时长的静音 MP3（兜底）"""
    try:
        subprocess.run([
            FFMPEG_BIN, "-y", "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(duration), "-q:a", "9", "-acodec", "libmp3lame",
            str(out_path)
        ], check=True, capture_output=True)
    except Exception as e:
        log.error(f"无法生成静音音频：{e}")


# ============ 4. 数字人视频合成 ============

def _is_sadtalker_available() -> bool:
    """检查 SadTalker 是否已安装"""
    sadtalker_dir = BASE_DIR / "sadtalker"
    inference_py = sadtalker_dir / "inference.py"
    return inference_py.exists()


def _generate_talking_head(avatar_img: str, audio_path: str,
                           result_dir: str) -> Optional[str]:
    """
    用 SadTalker 生成对口型数字人视频。
    输入：人物照片 + 语音
    输出：说话视频文件路径，失败返回 None

    CPU 模式下约需 5-10 分钟/页。
    """
    if not _is_sadtalker_available():
        log.warning("SadTalker 未安装，跳过 AI 数字人生成")
        return None

    wrapper = BASE_DIR / "digital_human" / "sadtalker_wrapper.py"
    if not wrapper.exists():
        log.warning(f"Sadtalker wrapper 不存在: {wrapper}")
        return None

    Path(result_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(wrapper),
        "--source_image", str(avatar_img),
        "--driven_audio", str(audio_path),
        "--result_dir", str(result_dir),
        "--full_body"
    ]

    log.info(f"[SadTalker] 开始生成对口型数字人...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900  # 15分钟超时
        )
    except subprocess.TimeoutExpired:
        log.error("[SadTalker] 生成超时（超过15分钟）")
        return None
    except Exception as e:
        log.error(f"[SadTalker] 调用异常: {e}")
        return None

    if result.returncode != 0:
        err = result.stderr[:1000] if result.stderr else "未知错误"
        log.error(f"[SadTalker] 生成失败: {err}")
        return None

    # 从 stdout 找到 RESULT: 行
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            video_path = line[7:].strip()
            if Path(video_path).exists():
                log.info(f"[SadTalker] 数字人视频生成成功: {video_path}")
                return video_path

    # 兜底：在 result_dir 里找最新的 mp4
    mp4_files = sorted(
        Path(result_dir).rglob("*.mp4"),
        key=lambda f: f.stat().st_mtime, reverse=True
    )
    if mp4_files:
        return str(mp4_files[0])

    log.error("[SadTalker] 未找到生成的视频文件")
    return None


def render_scene_video(scene: Scene, out_path: str, avatar: str,
                       ratio: str, resolution: str,
                       digital_human_mode: str = "auto") -> str:
    """
    把"PPT 页面 + 数字人 + 语音"合成一段视频。

    数字人模式（三级回退）:
      1. SadTalker AI 对口型（mode=auto 且 SadTalker 已安装）→ 真人说话视频
      2. 静态头像叠加（兜底）→ 右下角静态头像 + 语音
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    w, h = _ratio_size(ratio, resolution)

    # 选数字人头像（没有就用默认占位图）
    avatar_img = AVATAR_DIR / f"{avatar}.png"
    if not avatar_img.exists():
        avatar_img = _make_placeholder_avatar(avatar, w, h)

    # PPT 页面作为底图
    bg = scene.image_path if scene.image_path and Path(scene.image_path).exists() else None
    audio_dur = max(scene.duration_sec, 2.0)

    # 音频
    audio_path = scene.audio_path if scene.audio_path and Path(scene.audio_path).exists() else None

    # ========== 三级回退：尝试 SadTalker AI 数字人 ==========
    talking_head_video = None
    if digital_human_mode == "auto" and audio_path and _is_sadtalker_available():
        log.info(f"[渲染] 尝试 SadTalker AI 对口型数字人...")
        talking_head_video = _generate_talking_head(
            str(avatar_img),
            audio_path,
            str(out_path.parent / f"talking_head_{scene.index}")
        )

    if talking_head_video:
        # ========== 方案 A：SadTalker 对口型数字人 ==========
        # 把 SadTalker 生成的说话视频叠加到 PPT 背景上
        log.info(f"[渲染] 使用 SadTalker 数字人视频合成")

        if bg:
            bg_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(bg)]
        else:
            bg_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", f"color=c=0x1a1f3a:s={w}x{h}:r=25"
            ]

        # 数字人视频尺寸（右下角，比静态头像稍大）
        avatar_w = int(w * 0.28)
        avatar_h = int(avatar_w * 0.75)  # 4:3 比例
        avatar_x = w - avatar_w - int(w * 0.03)
        avatar_y = h - avatar_h - int(h * 0.03)

        # 音频输入
        if audio_path:
            audio_input = ["-i", str(audio_path)]
            audio_map = "2:a"
        else:
            audio_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"
            ]
            audio_map = "2:a"

        filter_complex = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=25[bg];"
            f"[1:v]scale={avatar_w}:{avatar_h},fps=25,format=yuva420p[avt];"
            f"[bg][avt]overlay={avatar_x}:{avatar_y}[v]"
        )

        cmd = [
            FFMPEG_BIN, "-y",
            *bg_input,
            "-i", str(talking_head_video),
            *audio_input,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", audio_map,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-t", str(audio_dur),
            str(out_path)
        ]

    else:
        # ========== 方案 B：静态头像（左右分栏，不遮挡 PPT） ==========
        log.info(f"[渲染] 使用静态头像左右分栏模式（不遮挡 PPT）")

        if bg:
            bg_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(bg)]
        else:
            bg_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", f"color=c=0x1a1f3a:s={w}x{h}:r=25"
            ]

        # 左右分栏：PPT 左侧主区域（75%），头像右侧栏（25%），完全不重叠
        # 避免头像叠加在 PPT 上遮挡文字
        main_w = int(w * 0.75)                   # PPT 主区域宽度
        bg_h = int(main_w * h / w)               # PPT 高度（保持原宽高比）
        bg_y = (h - bg_h) // 2                   # PPT 垂直居中
        side_w = w - main_w                      # 侧栏宽度
        avatar_w = int(side_w * 0.7)             # 头像大小（侧栏宽度的 70%）
        avatar_h = avatar_w
        avatar_x = main_w + (side_w - avatar_w) // 2  # 头像在侧栏水平居中
        avatar_y = (h - avatar_h) // 2                # 头像垂直居中
        avatar_input = [
            "-loop", "1", "-t", str(audio_dur),
            "-i", str(avatar_img)
        ]

        if audio_path:
            audio_input = ["-i", str(audio_path)]
            audio_map = "2:a"
        else:
            audio_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"
            ]
            audio_map = "2:a"

        # PPT 缩放到主区域并保持比例（加黑边），再 pad 到整画布右侧填纯色
        # 头像叠加在右侧侧栏居中，与 PPT 区域无重叠
        filter_complex = (
            f"[0:v]scale={main_w}:{bg_h}:force_original_aspect_ratio=decrease,"
            f"pad={main_w}:{bg_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=25[bg1];"
            f"[bg1]pad={w}:{h}:0:{bg_y}:0x1a1f3a[bg];"
            f"[1:v]scale={avatar_w}:{avatar_h},fps=25[avt];"
            f"[bg][avt]overlay={avatar_x}:{avatar_y}[v]"
        )

        cmd = [
            FFMPEG_BIN, "-y",
            *bg_input,
            *avatar_input,
            *audio_input,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", audio_map,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-t", str(audio_dur),
            str(out_path)
        ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors='ignore') if e.stderr else ''
        log.error(f"FFmpeg 渲染失败 (exit {e.returncode})：{err[:1000]}")
        raise
    except FileNotFoundError:
        raise RuntimeError("未找到 ffmpeg")

    return str(out_path)


def _ratio_size(ratio: str, resolution: str) -> tuple:
    presets = {
        ("16:9", "720p"): (1280, 720),
        ("16:9", "1080p"): (1920, 1080),
        ("9:16", "720p"): (720, 1280),
        ("9:16", "1080p"): (1080, 1920),
        ("1:1", "720p"): (720, 720),
        ("1:1", "1080p"): (1080, 1080),
    }
    return presets.get((ratio, resolution), (1280, 720))


def _make_placeholder_avatar(name: str, w: int, h: int) -> Path:
    """用 PIL 生成一个渐变圆形占位头像（不需要 cairo 库）"""
    png_path = AVATAR_DIR / f"{name}.png"
    if png_path.exists():
        return png_path
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return png_path  # 后续渲染会兜底

    palettes = {
        "teacher_female": ("#6c8cff", "#3a4ba0", "教师·女"),
        "teacher_male":   ("#3a4ba0", "#1f2a6b", "教师·男"),
        "young_girl":     ("#ff6ba0", "#b0306b", "少女"),
        "young_boy":      ("#36d6c0", "#1f8a7a", "少年"),
        "professor":      ("#b06bff", "#5a2da0", "教授"),
    }
    c1, c2, label = palettes.get(name, ("#6c8cff", "#3a4ba0", name))

    img = Image.new("RGB", (400, 400), c1)
    d = ImageDraw.Draw(img)
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    for y in range(400):
        t = y / 400
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        d.line([(0, y), (400, y)], fill=(r, g, b))
    d.ellipse([130, 100, 270, 240], fill="#ffe2c8")
    d.polygon([(100, 280), (300, 280), (320, 400), (80, 400)], fill="#ffe2c8")
    d.ellipse([165, 165, 180, 180], fill="#1a1a1a")
    d.ellipse([220, 165, 235, 180], fill="#1a1a1a")
    d.arc([180, 195, 220, 220], start=0, end=180, fill="#1a1a1a", width=4)
    try:
        font = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    d.text((200, 30), label, fill="white", anchor="mt", font=font)
    img.save(png_path, "PNG")
    return png_path


# ============ 5. 合并所有分镜为成片 ============
def concatenate_scenes(scene_videos: List[str], audio_track: Optional[str],
                       bgm: Optional[str], out_path: str,
                       subtitle: bool = False) -> str:
    """把多个分镜视频拼接为成片，可选叠加 BGM"""
    if not scene_videos:
        raise ValueError("没有可拼接的分镜视频")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 先拼接所有分镜
    concat_list = out_path.parent / "_concat.txt"
    with concat_list.open("w", encoding="utf-8") as f:
        for v in scene_videos:
            f.write(f"file '{v.replace(chr(92), '/')}'\n")

    tmp_concat = out_path.with_suffix(".concat.mp4")
    subprocess.run([
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy", str(tmp_concat)
    ], check=True, capture_output=True)

    # 叠加 BGM
    if bgm and Path(bgm).exists():
        bgm_vol = 0.15
        # 主音频音量降低，叠加 BGM
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-i", str(tmp_concat),
            "-i", str(bgm),
            "-filter_complex",
            f"[0:a]volume=0.9[a0];[1:a]volume={bgm_vol},afade=t=in:st=0:d=2,afade=t=out:st=-3:d=3[a1];"
            f"[a0][a1]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(out_path)
        ], check=True, capture_output=True)
        tmp_concat.unlink(missing_ok=True)
    else:
        tmp_concat.rename(out_path)

    return str(out_path)


# ============ 6. 任务主流程 ============
def run_job(job_id: str, executor: ThreadPoolExecutor):
    """任务入口（在后台线程池中执行）"""
    job = store.get(job_id)
    if not job:
        return
    job.started_at = time.time()
    store.update(job_id, status=JOB_STATUS_RUNNING, stage="解析 PPT")

    try:
        # Step 1: 解析 PPT
        log.info(f"[{job_id}] 开始解析 PPT")
        scenes = parse_pptx(job.pptx_path)
        job.scenes = scenes
        store.update(job_id, scenes=scenes, progress=0.15, stage=f"已解析 {len(scenes)} 页分镜")

        # Step 2: 生成讲解稿 + TTS
        log.info(f"[{job_id}] 生成讲解稿并合成 TTS")
        work_dir = OUTPUT_DIR / job_id
        audio_dir = work_dir / "audio"
        video_dir = work_dir / "scenes"
        final_dir = work_dir / "final"
        for d in [work_dir, audio_dir, video_dir, final_dir]:
            d.mkdir(parents=True, exist_ok=True)

        for i, scene in enumerate(scenes):
            if store.get(job_id).status == JOB_STATUS_CANCELED:
                raise RuntimeError("任务被取消")
            scene.script = generate_script(scene)
            audio_file = audio_dir / f"scene_{i:03d}.mp3"
            scene.duration_sec = tts_synthesize(scene.script, job.voice, str(audio_file))
            scene.audio_path = str(audio_file)
            store.update(job_id, scenes=scenes,
                         progress=0.15 + 0.25 * (i + 1) / len(scenes),
                         stage=f"TTS 第 {i + 1}/{len(scenes)} 页")
            log.info(f"[{job_id}] TTS {i + 1}/{len(scenes)} done: {scene.duration_sec:.1f}s")

        # Step 3: 渲染每个分镜视频
        log.info(f"[{job_id}] 渲染分镜视频")
        scene_videos: List[str] = []
        for i, scene in enumerate(scenes):
            if store.get(job_id).status == JOB_STATUS_CANCELED:
                raise RuntimeError("任务被取消")
            v = video_dir / f"scene_{i:03d}.mp4"
            render_scene_video(scene, str(v), job.avatar, job.ratio,
                               job.resolution, job.digital_human_mode)
            scene_videos.append(str(v))
            store.update(job_id,
                         progress=0.4 + 0.45 * (i + 1) / len(scenes),
                         stage=f"渲染第 {i + 1}/{len(scenes)} 页")
            log.info(f"[{job_id}] 渲染 {i + 1}/{len(scenes)} done")

        # Step 4: 合并成片
        log.info(f"[{job_id}] 合并成片")
        bgm_file = BGM_DIR / "default.mp3" if job.enable_bgm else None
        if bgm_file and not bgm_file.exists():
            bgm_file = None
        final = final_dir / f"{Path(job.filename).stem}.mp4"
        concatenate_scenes(scene_videos, None, str(bgm_file) if bgm_file else None,
                            str(final), subtitle=job.enable_subtitle)

        job.output_path = str(final)
        job.finished_at = time.time()
        store.update(job_id,
                     status=JOB_STATUS_DONE,
                     output_path=job.output_path,
                     progress=1.0,
                     stage="完成",
                     finished_at=job.finished_at)
        log.info(f"[{job_id}] 完成 → {final}")

    except Exception as e:
        log.exception(f"[{job_id}] 失败：{e}")
        store.update(job_id, status=JOB_STATUS_FAILED, error=str(e), stage="失败")


def submit_job(job: Job, executor: ThreadPoolExecutor) -> str:
    store.add(job)
    executor.submit(run_job, job.job_id, executor)
    return job.job_id


# ============ 测试入口 ============
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：python core.py <pptx 文件>")
        sys.exit(1)
    pptx = sys.argv[1]
    ex = ThreadPoolExecutor(max_workers=2)
    job = Job(
        job_id=uuid.uuid4().hex[:8],
        filename=Path(pptx).name,
        pptx_path=str(Path(pptx).resolve()),
    )
    submit_job(job, ex)
    print(f"任务已提交：{job.job_id}")
    while True:
        j = store.get(job.job_id)
        print(f"  {j.status} | {j.progress:.0%} | {j.stage}")
        if j.status in (JOB_STATUS_DONE, JOB_STATUS_FAILED):
            break
        time.sleep(2)
