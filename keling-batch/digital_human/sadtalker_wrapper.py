"""
SadTalker 数字人调用封装
=======================
这个脚本独立运行在 SadTalker 的目录中，通过命令行参数接收：
  --source_image  人物照片路径
  --driven_audio  语音文件路径
  --result_dir     输出目录
  --full_body      是否生成全身模式（默认是）

生成一个对口型的数字人视频，返回视频路径。

被 core.py 通过 subprocess 调用，不直接 import。
"""
import sys
import os
import shutil
from pathlib import Path


def run_sadtalker(source_image: str, driven_audio: str,
                  result_dir: str, full_body: bool = True) -> str:
    """
    调用 SadTalker 生成对口型数字人视频。

    参数:
        source_image: 人物正面照片路径
        driven_audio: 语音文件路径（wav/mp3）
        result_dir:   输出目录
        full_body:    True=全身模式（自然），False=仅面部

    返回:
        生成的视频文件路径，失败返回空字符串
    """
    # SadTalker 的根目录（本脚本应该放在 keling-batch/digital_human/ 下）
    # SadTalker 仓库在 keling-batch/sadtalker/
    script_dir = Path(__file__).resolve().parent
    sadtalker_dir = script_dir.parent / "sadtalker"

    if not sadtalker_dir.exists():
        print(f"[错误] SadTalker 未安装：{sadtalker_dir} 不存在")
        print("请先运行 setup_sadtalker.bat")
        return ""

    inference_py = sadtalker_dir / "inference.py"
    if not inference_py.exists():
        print(f"[错误] 找不到 {inference_py}")
        return ""

    # 切换到 SadTalker 目录（它的代码依赖相对路径）
    os.chdir(str(sadtalker_dir))

    # 把输入文件复制到 SadTalker 目录下（避免路径问题）
    tmp_img = sadtalker_dir / "tmp_input.png"
    tmp_audio = sadtalker_dir / "tmp_input.wav"
    shutil.copy2(source_image, tmp_img)

    # SadTalker 需要 wav 格式的音频
    # 如果是 mp3，用 ffmpeg 转换
    audio_ext = Path(driven_audio).suffix.lower()
    if audio_ext == ".wav":
        shutil.copy2(driven_audio, tmp_audio)
    else:
        import subprocess
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            ffmpeg = "ffmpeg"
        subprocess.run([
            ffmpeg, "-y", "-i", driven_audio,
            "-ar", "16000", "-ac", "1", str(tmp_audio)
        ], capture_output=True, check=True)

    # 构建 SadTalker 命令行参数
    cmd_args = [
        sys.executable, "inference.py",
        "--source_image", str(tmp_img),
        "--driven_audio", str(tmp_audio),
        "--result_dir", result_dir,
        "--enhancer", "gfpgan",
    ]

    if full_body:
        cmd_args.extend(["--still", "--preprocess", "full"])
    else:
        cmd_args.extend(["--preprocess", "crop"])

    print(f"[SadTalker] 开始生成数字人视频...")
    print(f"[SadTalker] 图片: {source_image}")
    print(f"[SadTalker] 音频: {driven_audio}")
    print(f"[SadTalker] 命令: {' '.join(cmd_args)}")

    import subprocess

    # 运行 SadTalker（CPU 模式可能需要 5-10 分钟）
    try:
        result = subprocess.run(
            cmd_args,
            cwd=str(sadtalker_dir),
            capture_output=True,
            text=True,
            timeout=900  # 15 分钟超时
        )
    except subprocess.TimeoutExpired:
        print("[错误] SadTalker 超时（超过 15 分钟）")
        _cleanup(tmp_img, tmp_audio)
        return ""

    if result.returncode != 0:
        print(f"[错误] SadTalker 运行失败 (exit {result.returncode})")
        print(f"[stderr] {result.stderr[:2000]}")
        _cleanup(tmp_img, tmp_audio)
        return ""

    # SadTalker 输出在 result_dir 下的一个时间戳子目录里
    # 找到最新的 .mp4 文件
    result_path = Path(result_dir)
    mp4_files = sorted(result_path.rglob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)

    if not mp4_files:
        print("[错误] SadTalker 未生成视频文件")
        _cleanup(tmp_img, tmp_audio)
        return ""

    output_video = str(mp4_files[0])
    print(f"[SadTalker] 生成成功：{output_video}")

    _cleanup(tmp_img, tmp_audio)
    return output_video


def _cleanup(*files):
    """清理临时文件"""
    for f in files:
        try:
            if Path(f).exists():
                Path(f).unlink()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SadTalker 数字人封装")
    parser.add_argument("--source_image", required=True, help="人物照片路径")
    parser.add_argument("--driven_audio", required=True, help="语音文件路径")
    parser.add_argument("--result_dir", required=True, help="输出目录")
    parser.add_argument("--full_body", action="store_true", default=True, help="全身模式")
    args = parser.parse_args()

    video_path = run_sadtalker(
        args.source_image,
        args.driven_audio,
        args.result_dir,
        args.full_body
    )

    if video_path:
        # 输出到 stdout，供调用方读取
        print(f"RESULT:{video_path}")
        sys.exit(0)
    else:
        sys.exit(1)
