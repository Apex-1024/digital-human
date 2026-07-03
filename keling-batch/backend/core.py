"""课灵 AI 批量制课系统 — 后端核心"""
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
from concurrent.futures import ThreadPoolExecutor

# 重定向模型/配置目录到项目内，避免沙箱权限问题
_MODEL_ROOT = str(Path(__file__).resolve().parent.parent / ".p2t_models")
os.environ.setdefault("PIX2TEXT_HOME", _MODEL_ROOT)
os.environ.setdefault("CNSTD_HOME", _MODEL_ROOT)
os.environ.setdefault("CNOCR_HOME", _MODEL_ROOT)
os.environ.setdefault("YOLO_CONFIG_DIR", _MODEL_ROOT)
os.environ.setdefault("ULTRALYTICS_CONFIG_DIR", _MODEL_ROOT)
os.environ.setdefault("HF_HOME", _MODEL_ROOT)
os.environ.setdefault("XDG_CACHE_HOME", _MODEL_ROOT)

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

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("keling-batch")


def _find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(path).exists():
            log.info(f"使用 ffmpeg: {path}")
            return path
    except ImportError:
        pass
    return "ffmpeg"


FFMPEG_BIN = _find_ffmpeg()

_p2t_engine = None


def _get_p2t():
    """懒加载 Pix2Text 引擎（文字+公式一体化识别）"""
    global _p2t_engine
    if _p2t_engine is not None:
        return _p2t_engine
    if _p2t_engine is False:
        return None
    try:
        from pix2text import Pix2Text
        _p2t_engine = Pix2Text()
        log.info("Pix2Text 初始化成功")
    except Exception as e:
        log.warning(f"Pix2Text 初始化失败：{e}")
        _p2t_engine = False
    return _p2t_engine


def _ocr_slide_image(image_path: str) -> List[tuple]:
    """用 Pix2Text 识别整页 PPT 渲染图，返回 [(text, kind, y), ...]，按 y 升序

    pix2text 输出 Markdown 格式，公式用 $...$ 包裹。
    按行拆分，含 $ 的行标记为 formula，其余为 text。
    y 坐标按行号递增（pix2text 不返回精确 y 坐标）。
    """
    result: List[tuple] = []
    p2t = _get_p2t()
    if not p2t:
        return result

    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        img_h = img.height
    except Exception as e:
        log.warning(f"打开图片失败 {image_path}：{e}")
        return result

    try:
        md_result = p2t.recognize(image_path)
    except Exception as e:
        log.warning(f"Pix2Text 识别失败 {image_path}：{e}")
        return result

    if not md_result:
        return result

    lines = str(md_result).split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        y = i * (img_h // max(len(lines), 1))
        if "$" in line:
            formulas = re.findall(r"\$\$(.*?)\$\$|\$(.*?)\$", line)
            if formulas:
                for block in formulas:
                    latex = block[0] or block[1]
                    latex = latex.strip()
                    if latex:
                        result.append((latex, "formula", y))
            else:
                result.append((line.replace("$", "").strip(), "text", y))
        else:
            result.append((line, "text", y))

    result.sort(key=lambda x: x[2])
    return result


def latex_to_chinese(latex: str) -> str:
    """LaTeX 公式 → 中文口播读法"""
    if not latex:
        return ""
    s = latex.strip().strip("$").strip()

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
    # 单字符下标（如 T_{2} → T2）直接拼接，更自然；多字符下标才读"下标"
    s = re.sub(r"\^\{([^{}]+)\}", r" 的 \1 次方", s)
    s = re.sub(r"_\{([0-9A-Za-z])\}", r"\1", s)  # 单字符下标直接拼接
    s = re.sub(r"_\{([^{}]+)\}", r" 下标 \1", s)  # 多字符下标读"下标"
    s = re.sub(r"\^([0-9A-Za-z])", r" 的 \1 次方", s)
    s = re.sub(r"_([0-9A-Za-z])", r"\1", s)  # 单字符下标直接拼接

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
    # \stackrel{a}{b} → b 上方 a（简化处理）
    s = re.sub(r"\\stackrel\{([^{}]*)\}\{([^{}]*)\}", r" \2 上方 \1 ", s)
    # \mathrm{...} \mathbb{...} 等字体修饰去掉（在后面统一处理）
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


def math_symbols_to_chinese(text: str) -> str:
    """数学符号 → 中文口播读法"""
    if not text:
        return ""
    s = text

    # MathType Symbol 字体 Private Use Area 字符映射
    mathtype_map = {
        "\uf021": "±",   # plus-minus
        "\uf022": "≥",   # greaterequal
        "\uf023": "≤",   # lessequal
        "\uf024": "∫",   # integral
        "\uf029": "∞",   # infinity
        "\uf02a": "∂",   # partial
        "\uf02b": "∏",   # product
        "\uf02d": "-",   # minus (Symbol 字体 0x2D)
        "\uf02f": "·",   # middle dot
        "\uf030": "√",   # sqrt
        "\uf034": "∑",   # summation
        "\uf035": "∏",   # product
        "\uf03d": "=",   # equal (Symbol 字体 0x3D)
        "\uf03f": "≈",   # approx
        "\uf040": "≡",   # equiv
        "\uf041": "α",   # alpha
        "\uf042": "β",   # beta
        "\uf043": "×",   # multiply (Symbol 0xB4 → × 实际 0xD7 但映射到这里)
        "\uf045": "ε",   # epsilon
        "\uf046": "φ",   # phi
        "\uf047": "γ",   # gamma
        "\uf048": "η",   # eta
        "\uf049": "ι",   # iota
        "\uf04a": "φ",   # varphi
        "\uf04b": "κ",   # kappa
        "\uf04c": "λ",   # lambda
        "\uf04d": "μ",   # mu
        "\uf04e": "ν",   # nu
        "\uf04f": "ο",   # omicron
        "\uf050": "π",   # pi
        "\uf051": "θ",   # theta
        "\uf052": "ρ",   # rho
        "\uf053": "σ",   # sigma
        "\uf054": "τ",   # tau
        "\uf055": "υ",   # upsilon
        "\uf056": "→",   # arrow
        "\uf057": "ω",   # omega
        "\uf058": "ξ",   # xi
        "\uf059": "ψ",   # psi
        "\uf05a": "ζ",   # zeta
        "\uf0a3": "≤",   # lessequal (Symbol 0xA3)
        "\uf0b3": "≥",   # greaterequal (Symbol 0xB3)
        "\uf0b4": "≠",   # notequal (Symbol 0xB4)
        "\uf0b7": "·",   # centerdot
        "\uf0c5": "×",   # multiply (Symbol 0xC5)
        "\uf0c6": "÷",   # divide (Symbol 0xC6)
        "\uf0d0": "→",   # rightarrow (Symbol 0xD0)
        "\uf0de": "→",   # arrow
        "\uf0e5": "π",   # pi (Symbol 0xE5)
        "\uf0e9": "∇",   # nabla
        "\uf0f4": "√",   # sqrt (Symbol 0xF4)
        "\uf0f5": "∝",   # proportional
        "\uf0f6": "∞",   # infinity
    }
    for k, v in mathtype_map.items():
        s = s.replace(k, v)

    # ---- 复合关系符（先于单字符处理，避免误替换）----
    s = s.replace("≥", " 大于等于 ").replace("≦", " 小于等于 ")
    s = s.replace("≤", " 小于等于 ").replace("≧", " 大于等于 ")
    s = s.replace("≠", " 不等于 ").replace("≈", " 约等于 ")
    s = s.replace("≡", " 恒等于 ").replace("∝", " 正比于 ")
    s = s.replace("→", " 趋向 ").replace("←", " 反向趋向 ")
    s = s.replace("∞", " 无穷 ")
    s = s.replace("±", " 正负 ").replace("∓", " 负正 ")
    s = s.replace("√", " 根号 ").replace("∑", " 求和 ").replace("∏", " 求积 ")
    s = s.replace("∈", " 属于 ").replace("∉", " 不属于 ")
    s = s.replace("∪", " 并集 ").replace("∩", " 交集 ")
    s = s.replace("∀", " 任意 ").replace("∃", " 存在 ")
    s = s.replace("∂", " 偏导 ").replace("∇", " 梯度 ")

    # ---- 区间括号 [a, b] → a 到 b ----
    # [T1,T2] → T1 到 T2 ； [a, b] → a 到 b
    s = re.sub(r"\[\s*([^,\]\[]+?)\s*,\s*([^,\]\[]+?)\s*\]", r" \1 到 \2 ", s)

    # ---- 积分号（如果 OCR 直接识别到 ∫）----
    # ∫_a^b → 从 a 到 b 积分 ； ∫ → 积分
    s = re.sub(r"∫[_\s]*([0-9A-Za-z]+)\s*\^\s*([0-9A-Za-z]+)", r" 从 \1 到 \2 积分 ", s)
    s = s.replace("∫", " 积分 ")

    # ---- 算术运算符 ----
    # 减号：在字母/数字之间用"减"，否则去掉
    s = re.sub(r"([0-9A-Za-z）)])\s*[-−－]\s*([0-9A-Za-z（(])", r"\1 减 \2", s)
    # 加号
    s = re.sub(r"([0-9A-Za-z）)])\s*\+\s*([0-9A-Za-z（(])", r"\1 加 \2", s)
    s = s.replace("×", " 乘以 ").replace("·", " 乘 ").replace("·", " 乘 ")
    s = s.replace("÷", " 除以 ")

    # ---- 等号 ----
    s = s.replace("=", " 等于 ")

    # ---- 大于/小于（在复合符处理后）----
    s = re.sub(r"([0-9A-Za-z）)])\s*>\s*([0-9A-Za-z（(])", r"\1 大于 \2", s)
    s = re.sub(r"([0-9A-Za-z）)])\s*<\s*([0-9A-Za-z（(])", r"\1 小于 \2", s)

    # ---- 上标/下标数字（如 T1, T2, x2）----
    # 单字母+单数字（T1, T2, x2）直接保留，读作"T1""T2"（更自然）
    # 多字符下标才读"下标"（如 T12 → T 下标 12）
    s = re.sub(r"([A-Za-z])([0-9]{2,})", r"\1 下标 \2", s)

    # ---- 撇号（导数 s'(t) → s 撇 (t)）----
    s = s.replace("'", " 撇 ").replace("'", " 撇 ").replace("'", " 撇 ")

    # ---- 百分号 ----
    s = s.replace("%", " 百分之 ")

    # 折叠多空格
    s = re.sub(r"\s+", " ", s).strip()
    # 修正：中文与中文之间的多余空格
    s = re.sub(r"([\u4e00-\u9fff，。：；！？])\s+([\u4e00-\u9fff，。：；！？])", r"\1\2", s)
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
    """解析 PPT 每页幻灯片：python-pptx 取标题，Pix2Text 取正文+公式"""
    try:
        from pptx import Presentation
    except ImportError:
        raise RuntimeError("缺少 python-pptx，请先运行 pip install python-pptx")

    prs = Presentation(pptx_path)
    scenes: List[Scene] = []

    pptx_path = Path(pptx_path)
    slide_imgs_dir = pptx_path.parent / f"_slides_{pptx_path.stem}"
    slide_imgs_dir.mkdir(exist_ok=True)
    _convert_pptx_to_images(pptx_path, slide_imgs_dir)

    img_files = sorted(slide_imgs_dir.glob("slide_*.png"))

    for idx, slide in enumerate(prs.slides):
        title = ""
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    title = text
                    break
            if title:
                break

        img_path = str(img_files[idx]) if idx < len(img_files) else None
        bullets: List[str] = []

        if img_path and Path(img_path).exists():
            try:
                ocr_items = _ocr_slide_image(img_path)
                seen_norm = set()
                if title:
                    seen_norm.add(title.replace(" ", ""))
                for text, kind, _y in ocr_items:
                    norm = text.replace(" ", "")
                    if norm in seen_norm:
                        continue
                    if kind == "formula":
                        bullets.append(f"@@FORMULA@@{text}")
                    else:
                        bullets.append(text)
                    seen_norm.add(norm)
            except Exception as e:
                log.warning(f"第 {idx + 1} 页 OCR 失败：{e}")

        if not title:
            title = f"第 {idx + 1} 页"

        scenes.append(Scene(index=idx, title=title, bullets=bullets, image_path=img_path))

    return scenes


def _ppt_com_to_pdf(pptx_path: Path, pdf_path: Path) -> bool:
    """PowerPoint COM 自动化：PPT → PDF"""
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
        pres = app.Presentations.Open(abs_pptx, ReadOnly=True, WithWindow=False)
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
    """LibreOffice 转 PDF"""
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
        if proc.poll() is not None:
            break
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
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
        if elapsed > timeout:
            log.warning(f"LibreOffice 转换超时（{timeout}s）")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            shutil.rmtree(tmp_profile, ignore_errors=True)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                log.info("LibreOffice: 超时但 PDF 已生成")
                return True
            return False
        time.sleep(2)

    shutil.rmtree(tmp_profile, ignore_errors=True)
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        log.info(f"LibreOffice: PDF 生成成功（{time.time()-start:.0f}s）")
        return True
    log.warning(f"LibreOffice: 进程退出但无 PDF（exit={proc.returncode}）")
    return False


def _convert_pptx_to_images(pptx_path: Path, out_dir: Path):
    """PPT 每页转 PNG：PowerPoint COM → LibreOffice，均失败则抛异常"""
    pdf_path = out_dir.parent / (pptx_path.stem + ".pdf")

    try:
        subprocess.run(["taskkill", "/F", "/IM", "soffice.exe"],
                       capture_output=True, timeout=5)
        subprocess.run(["taskkill", "/F", "/IM", "soffice.bin"],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    if _ppt_com_to_pdf(pptx_path, pdf_path):
        _pdf_to_images(pdf_path, out_dir)
        if any(out_dir.glob("slide_*.png")):
            return

    if _soffice_to_pdf(pptx_path, pdf_path, timeout=180):
        time.sleep(1)
        _pdf_to_images(pdf_path, out_dir)
        if any(out_dir.glob("slide_*.png")):
            return

    raise RuntimeError(
        "PPT 转图片失败：PowerPoint COM 和 LibreOffice 均不可用。"
        "请安装 Microsoft Office 或 LibreOffice。"
    )


def _pdf_to_images(pdf_path: Path, out_dir: Path):
    """PDF 每页 → PNG"""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf_path))
        for i in range(len(doc)):
            bitmap = doc[i].render(scale=2.0)
            bitmap.to_pil().save(out_dir / f"slide_{i}.png", "PNG")
        log.info(f"pypdfium2: PDF → {len(doc)} 张 PNG")
        return
    except ImportError:
        log.warning("pypdfium2 未安装")
    except Exception as e:
        log.warning(f"pypdfium2 渲染失败：{e}")

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
    """把 title + bullets 拼成口播文案，公式转中文口播，超长截断不跨页"""
    parts = [math_symbols_to_chinese(scene.title) + "。"]
    for b in scene.bullets:
        if not b:
            continue
        if b.startswith("@@FORMULA@@"):
            latex = b[len("@@FORMULA@@"):]
            latex_fixed = latex.replace(r"\nu", "v")
            spoken = latex_to_chinese(latex_fixed)
            spoken = math_symbols_to_chinese(spoken)
            if spoken:
                parts.append(spoken + "。")
        else:
            b = b.strip().rstrip("。.")
            if b:
                parts.append(math_symbols_to_chinese(b) + "。")
    text = " ".join(parts)
    if len(text) > 400:
        text = text[:400] + "..."
    return text


# ============ 3. TTS 合成 ============
def tts_synthesize(text: str, voice: str, out_path: str) -> float:
    """edge-tts 合成语音，返回时长（秒），失败回退 pyttsx3"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

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

    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        if out_path.exists() and out_path.stat().st_size > 0:
            return _audio_duration(out_path)
    except Exception as e:
        log.warning(f"pyttsx3 失败：{e}")

    log.warning("TTS 全部失败，生成静音占位")
    _make_silent_audio(out_path, duration=3.0)
    return 3.0


def _audio_duration(audio_path: Path) -> float:
    """用 ffmpeg 探测音频时长"""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", str(audio_path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=10
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 3.0


def _make_silent_audio(out_path: Path, duration: float):
    """生成静音 MP3"""
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
    sadtalker_dir = BASE_DIR / "sadtalker"
    return (sadtalker_dir / "inference.py").exists()


def _generate_talking_head(avatar_img: str, audio_path: str,
                           result_dir: str) -> Optional[str]:
    """SadTalker 对口型数字人视频，失败返回 None"""
    if not _is_sadtalker_available():
        return None

    wrapper = BASE_DIR / "digital_human" / "sadtalker_wrapper.py"
    if not wrapper.exists():
        return None

    Path(result_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(wrapper),
        "--source_image", str(avatar_img),
        "--driven_audio", str(audio_path),
        "--result_dir", str(result_dir),
        "--full_body"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        log.error("[SadTalker] 生成超时")
        return None
    except Exception as e:
        log.error(f"[SadTalker] 调用异常: {e}")
        return None

    if result.returncode != 0:
        log.error(f"[SadTalker] 生成失败: {result.stderr[:500] if result.stderr else '未知'}")
        return None

    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            video_path = line[7:].strip()
            if Path(video_path).exists():
                return video_path

    mp4_files = sorted(
        Path(result_dir).rglob("*.mp4"),
        key=lambda f: f.stat().st_mtime, reverse=True
    )
    return str(mp4_files[0]) if mp4_files else None


def render_scene_video(scene: Scene, out_path: str, avatar: str,
                       ratio: str, resolution: str,
                       digital_human_mode: str = "auto") -> str:
    """合成 PPT 页面 + 数字人 + 语音 为一段视频"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    w, h = _ratio_size(ratio, resolution)

    avatar_img = AVATAR_DIR / f"{avatar}.png"
    if not avatar_img.exists():
        avatar_img = _make_placeholder_avatar(avatar, w, h)

    bg = scene.image_path if scene.image_path and Path(scene.image_path).exists() else None
    audio_dur = max(scene.duration_sec, 2.0)
    audio_path = scene.audio_path if scene.audio_path and Path(scene.audio_path).exists() else None

    talking_head_video = None
    if digital_human_mode == "auto" and audio_path and _is_sadtalker_available():
        talking_head_video = _generate_talking_head(
            str(avatar_img), audio_path,
            str(out_path.parent / f"talking_head_{scene.index}")
        )

    if talking_head_video:
        if bg:
            bg_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(bg)]
        else:
            bg_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", f"color=c=0x1a1f3a:s={w}x{h}:r=25"
            ]

        avatar_w = int(w * 0.28)
        avatar_h = int(avatar_w * 0.75)
        avatar_x = w - avatar_w - int(w * 0.03)
        avatar_y = h - avatar_h - int(h * 0.03)

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
            "-map", "[v]", "-map", audio_map,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-pix_fmt", "yuv420p", "-shortest",
            "-t", str(audio_dur),
            str(out_path)
        ]
    else:
        if bg:
            bg_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(bg)]
        else:
            bg_input = [
                "-f", "lavfi", "-t", str(audio_dur),
                "-i", f"color=c=0x1a1f3a:s={w}x{h}:r=25"
            ]

        main_w = int(w * 0.75)
        bg_h = int(main_w * h / w)
        bg_y = (h - bg_h) // 2
        side_w = w - main_w
        avatar_w = int(side_w * 0.7)
        avatar_h = avatar_w
        avatar_x = main_w + (side_w - avatar_w) // 2
        avatar_y = (h - avatar_h) // 2
        avatar_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(avatar_img)]

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
            "-map", "[v]", "-map", audio_map,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-pix_fmt", "yuv420p", "-shortest",
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
    """生成占位头像"""
    png_path = AVATAR_DIR / f"{name}.png"
    if png_path.exists():
        return png_path
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return png_path

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


# ============ 5. 合并分镜 ============
def concatenate_scenes(scene_videos: List[str], audio_track: Optional[str],
                       bgm: Optional[str], out_path: str,
                       subtitle: bool = False) -> str:
    """拼接分镜视频，可选叠加 BGM"""
    if not scene_videos:
        raise ValueError("没有可拼接的分镜视频")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    concat_list = out_path.parent / "_concat.txt"
    with concat_list.open("w", encoding="utf-8") as f:
        for v in scene_videos:
            f.write(f"file '{v.replace(chr(92), '/')}'\n")

    tmp_concat = out_path.with_suffix(".concat.mp4")
    subprocess.run([
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(tmp_concat)
    ], check=True, capture_output=True)

    if bgm and Path(bgm).exists():
        subprocess.run([
            FFMPEG_BIN, "-y",
            "-i", str(tmp_concat), "-i", str(bgm),
            "-filter_complex",
            f"[0:a]volume=0.9[a0];[1:a]volume=0.15,afade=t=in:st=0:d=2,afade=t=out:st=-3:d=3[a1];"
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
    """任务入口（后台线程池执行）"""
    job = store.get(job_id)
    if not job:
        return
    job.started_at = time.time()
    store.update(job_id, status=JOB_STATUS_RUNNING, stage="解析 PPT")

    try:
        log.info(f"[{job_id}] 开始解析 PPT")
        scenes = parse_pptx(job.pptx_path)
        job.scenes = scenes
        store.update(job_id, scenes=scenes, progress=0.15, stage=f"已解析 {len(scenes)} 页分镜")

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

        log.info(f"[{job_id}] 合并成片")
        bgm_file = BGM_DIR / "default.mp3" if job.enable_bgm else None
        if bgm_file and not bgm_file.exists():
            bgm_file = None
        final = final_dir / f"{Path(job.filename).stem}.mp4"
        concatenate_scenes(scene_videos, None, str(bgm_file) if bgm_file else None,
                            str(final), subtitle=job.enable_subtitle)

        job.output_path = str(final)
        job.finished_at = time.time()
        store.update(job_id, status=JOB_STATUS_DONE, output_path=job.output_path,
                     progress=1.0, stage="完成", finished_at=job.finished_at)
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
