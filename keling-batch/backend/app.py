"""
课灵 AI 批量制课系统 — Flask API
提供：
  POST /api/upload          上传 PPT
  POST /api/jobs            创建制课任务
  GET  /api/jobs            列出所有任务
  GET  /api/jobs/<id>       查询任务详情
  POST /api/jobs/<id>/cancel 取消任务
  GET  /api/jobs/<id>/download 下载成片
  DELETE /api/jobs/<id>     删除任务
  GET  /api/avatars         数字人列表
  GET  /api/voices          音色列表
"""
import os
import uuid
import json
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import (
    Job, store, submit_job, BASE_DIR, UPLOAD_DIR, OUTPUT_DIR, ASSETS_DIR,
    AVATAR_DIR, BGM_DIR, JobStore
)

log = logging.getLogger("keling-api")

app = Flask(__name__, static_folder=str(BASE_DIR / "frontend" / "static"),
            template_folder=str(BASE_DIR / "frontend" / "templates"))
CORS(app)

# 全局线程池（最多同时跑 2 个制课任务，避免机器卡死）
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="keling-job")

ALLOWED_EXT = {".pptx", ".ppt"}
MAX_SIZE_MB = 200


# ============ 数字人 / 音色配置 ============
AVATARS = [
    {"id": "teacher_female", "name": "女教师·亲和", "preview": "teacher_female.png"},
    {"id": "teacher_male", "name": "男教师·沉稳", "preview": "teacher_male.png"},
    {"id": "young_girl", "name": "少女·活泼", "preview": "young_girl.png"},
    {"id": "young_boy", "name": "少年·阳光", "preview": "young_boy.png"},
    {"id": "professor", "name": "教授·权威", "preview": "professor.png"},
]

VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 (女·温柔)", "gender": "female", "lang": "zh-CN"},
    {"id": "zh-CN-YunxiNeural", "name": "云希 (男·沉稳)", "gender": "male", "lang": "zh-CN"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬 (男·专业)", "gender": "male", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊 (女·活力)", "gender": "female", "lang": "zh-CN"},
    {"id": "zh-CN-YunjianNeural", "name": "云健 (男·体育)", "gender": "male", "lang": "zh-CN"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北 (女·东北话)", "gender": "female", "lang": "zh-CN"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮 (女·陕西方言)", "gender": "female", "lang": "zh-CN"},
    {"id": "en-US-JennyNeural", "name": "Jenny (English)", "gender": "female", "lang": "en-US"},
]


# ============ 路由 ============
@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


@app.route("/api/avatars")
def list_avatars():
    return jsonify({"avatars": AVATARS})


@app.route("/api/voices")
def list_voices():
    return jsonify({"voices": VOICES})


@app.route("/api/digital_human_status")
def digital_human_status():
    """检查 SadTalker 是否已安装"""
    sadtalker_dir = BASE_DIR / "sadtalker"
    inference_py = sadtalker_dir / "inference.py"
    installed = inference_py.exists()
    return jsonify({
        "sadtalker_installed": installed,
        "mode": "auto" if installed else "static"
    })


@app.route("/api/upload", methods=["POST"])
def upload_ppt():
    if "file" not in request.files:
        return jsonify({"error": "未提供文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的文件格式 {ext}，仅接受 {ALLOWED_EXT}"}), 400
    # 大小校验
    f.seek(0, os.SEEK_END)
    size_mb = f.tell() / 1024 / 1024
    f.seek(0)
    if size_mb > MAX_SIZE_MB:
        return jsonify({"error": f"文件过大 {size_mb:.1f}MB > {MAX_SIZE_MB}MB"}), 400

    safe_name = secure_filename(f.filename)
    save_path = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    f.save(save_path)
    log.info(f"已上传：{save_path} ({size_mb:.2f}MB)")

    return jsonify({
        "path": str(save_path),
        "filename": safe_name,
        "size_mb": round(size_mb, 2)
    })


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.get_json(force=True)
    pptx_path = data.get("pptx_path")
    if not pptx_path or not Path(pptx_path).exists():
        return jsonify({"error": "无效的 PPT 路径"}), 400

    job = Job(
        job_id=uuid.uuid4().hex[:8],
        filename=Path(pptx_path).name,
        pptx_path=pptx_path,
        avatar=data.get("avatar", "teacher_female"),
        voice=data.get("voice", "zh-CN-XiaoxiaoNeural"),
        ratio=data.get("ratio", "16:9"),
        resolution=data.get("resolution", "720p"),
        digital_human_mode=data.get("digital_human_mode", "auto"),
        enable_subtitle=bool(data.get("enable_subtitle", True)),
        enable_bgm=bool(data.get("enable_bgm", True)),
    )
    submit_job(job, executor)
    return jsonify({"job_id": job.job_id, "status": job.status})


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    return jsonify({"jobs": [j.to_dict() for j in store.all()]})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job.to_dict())


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = store.update(job_id, status="canceled", stage="已取消")
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    # 删除输出文件
    if job.output_path:
        Path(job.output_path).parent.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil_path = Path(job.output_path)
            for f in shutil_path.parent.iterdir():
                if f.is_file():
                    f.unlink()
        except Exception as e:
            log.warning(f"清理输出失败：{e}")
    store.delete(job_id)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/download")
def download_job(job_id):
    job = store.get(job_id)
    if not job or not job.output_path:
        return jsonify({"error": "任务未完成或无输出"}), 404
    return send_file(job.output_path, as_attachment=True,
                     download_name=Path(job.output_path).name)


@app.route("/api/avatars/<avatar_id>/preview")
def avatar_preview(avatar_id):
    p = AVATAR_DIR / f"{avatar_id}.png"
    if p.exists():
        return send_file(str(p))
    p = AVATAR_DIR / f"{avatar_id}.svg"
    if p.exists():
        return send_file(str(p))
    return send_file(str(AVATAR_DIR / "teacher_female.png"))


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ffmpeg": _check_ffmpeg()})


def _check_ffmpeg():
    import subprocess
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ff = "ffmpeg"
    try:
        r = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    import shutil
    # 检查 ffmpeg
    if not _check_ffmpeg():
        log.warning("⚠️  未检测到 ffmpeg，请先安装并加入 PATH")
    log.info("🚀 课灵 AI 批量制课系统启动")
    log.info(f"   上传目录：{UPLOAD_DIR}")
    log.info(f"   输出目录：{OUTPUT_DIR}")
    log.info(f"   资源目录：{ASSETS_DIR}")
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)
