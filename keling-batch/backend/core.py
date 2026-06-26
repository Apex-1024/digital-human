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

        # 兜底标题
        if not title:
            title = f"第 {idx + 1} 页"

        img_path = str(img_files[idx]) if idx < len(img_files) else None
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
    启发式生成讲解稿：把 title + bullets 串成一段口播。
    实际生产可对接 LLM（豆包 / 通义 / OpenAI）做润色。
    """
    parts = [scene.title + "。"]
    if scene.bullets:
        # 最多取 5 条要点
        for b in scene.bullets[:5]:
            b = b.strip().rstrip("。.")
            if b:
                parts.append(b + "。")
    text = " ".join(parts)
    # 限制长度（避免 TTS 过长）
    if len(text) > 200:
        text = text[:200] + "..."
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
def render_scene_video(scene: Scene, out_path: str, avatar: str,
                       ratio: str, resolution: str) -> str:
    """
    把"PPT 页面 + 数字人 + 语音"合成一段视频。
    数字人用占位方案：右下角叠加一个头像（带轻微缩放动画）。
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
    if bg:
        bg_input = ["-loop", "1", "-t", str(audio_dur), "-i", str(bg)]
    else:
        bg_input = [
            "-f", "lavfi", "-t", str(audio_dur),
            "-i", f"color=c=0x1a1f3a:s={w}x{h}:r=25"
        ]

    # 数字人头像尺寸（右下角）
    avatar_w = int(w * 0.22)
    avatar_h = avatar_w
    avatar_x = w - avatar_w - int(w * 0.04)
    avatar_y = h - avatar_h - int(h * 0.04)
    avatar_input = [
        "-loop", "1", "-t", str(audio_dur),
        "-i", str(avatar_img)
    ]

    # 音频
    if scene.audio_path and Path(scene.audio_path).exists():
        audio_input = ["-i", str(scene.audio_path)]
    else:
        audio_input = [
            "-f", "lavfi", "-t", str(audio_dur),
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"
        ]

    # 滤镜链：背景缩放 → 头像缩放 → 叠加 → 输出
    filter_complex = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=25[bg];"
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
        "-map", "2:a",
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
            render_scene_video(scene, str(v), job.avatar, job.ratio, job.resolution)
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
