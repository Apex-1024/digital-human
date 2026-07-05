# 课灵 AI · 批量制课系统

> 一个本地可运行的"PPT → 数字人讲解视频"批量生成器。
> 把任何 .pptx 拖进去，自动解析文字与数学公式，配上数字人和声音，生成口播视频，可批量并发。

## 快速开始

### Windows
双击 `start.bat`，等待依赖安装完成，浏览器自动打开 http://localhost:7860

### 手动启动
```powershell
cd keling-batch
pip install -r requirements.txt
python backend\app.py
```

服务监听 `0.0.0.0:7860`，按 `Ctrl+C` 停止。

### 环境变量（可选）

| 变量 | 作用 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API key，启用 LLM 公式口播转换；未设置则回退正则方案 | 空 |
| `MINERU_MODEL_SOURCE` | MinerU 模型下载源（`modelscope` / `huggingface`） | `modelscope` |

设置 DeepSeek key：
```powershell
setx DEEPSEEK_API_KEY "sk-你的key"
# 重启终端后生效
```

## 核心功能

| 步骤 | 做了什么 | 用到的技术 |
|---|---|---|
| ① PPT → PDF | PowerPoint COM 优先，LibreOffice 兜底 | pywin32 / soffice |
| ② PDF 解析 | 提取文字 + 数学公式（含 LaTeX），按页分镜 | MinerU 3.4（MFR 公式识别） |
| ③ PDF → PNG | 每页渲染为图片作为视频背景板 | pypdfium2 |
| ④ 口播文案 | 公式 LaTeX → 中文口播，DeepSeek LLM 优先、正则兜底 | DeepSeek API + 自研规则 |
| ⑤ TTS 合成 | 中文 7 种 + 英文 1 种，免费无限量 | edge-tts 7.2.8+ |
| ⑥ 数字人 | SadTalker 对口型优先，静态头像兜底 | SadTalker / ffmpeg |
| ⑦ 视频合成 | PPT 底图 + 数字人头像 + 语音 + BGM，音话同步 | ffmpeg（imageio-ffmpeg 静态二进制） |
| ⑧ 批量队列 | 同时跑 2 个任务，状态持久化 | ThreadPoolExecutor + jobs.json |

## 项目结构

```
keling-batch/
├── backend/
│   ├── app.py                       # Flask API（11 个接口）
│   └── core.py                      # 核心：解析 / TTS / 合成 / 队列 / 公式转换
├── digital_human/
│   └── sadtalker_wrapper.py         # SadTalker 数字人对口型封装
├── frontend/
│   ├── templates/index.html
│   └── static/
│       ├── style.css
│       └── app.js
├── assets/
│   ├── avatars/                     # 数字人头像（5 种内置 PNG/SVG）
│   └── bgm/                         # 背景音乐（放入 default.mp3 启用）
├── _mineru_patch/
│   └── usercustomize.py             # pypdfium2 5.x 兼容补丁（PdfImage.get_pos）
├── uploads/                         # 上传的 PPT 及中间产物（PDF/MinerU 解析/PNG）
├── output/                          # 生成的视频成片（按 job_id 分目录）
├── start.bat / start.sh
├── requirements.txt
└── jobs.json                        # 任务状态持久化
```

## 处理流程

```
PPTX
  │
  ▼  PowerPoint COM / LibreOffice
PDF ──────────────────────┐
  │                        │
  ▼  MinerU 解析           ▼  pypdfium2 渲染
文字 + 公式 LaTeX          每页 PNG（视频背景板）
  │
  ▼  DeepSeek LLM / 正则回退
中文口播文案
  │
  ▼  edge-tts
MP3 音频（驱动每页视频时长）
  │
  ▼  ffmpeg 合成
分镜 MP4（PPT 底图 + 数字人 + 语音）
  │
  ▼  ffmpeg concat（音话同步）
最终成片 MP4
```

**音话同步机制**：每页视频时长由该页 TTS 音频时长决定，ffmpeg 用 `-t ${audio_dur} -shortest` 保证音画对齐，`concat demuxer -c copy` 零漂移拼接。

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端工作台 |
| GET | `/api/health` | 健康检查（含 ffmpeg 状态） |
| POST | `/api/upload` | 上传 PPT（multipart/form-data，≤200MB） |
| POST | `/api/jobs` | 创建制课任务（JSON body） |
| GET | `/api/jobs` | 列出所有任务 |
| GET | `/api/jobs/<id>` | 查询单个任务 |
| POST | `/api/jobs/<id>/cancel` | 取消运行中的任务 |
| DELETE | `/api/jobs/<id>` | 删除任务及所有产物（uploads/ + output/） |
| GET | `/api/jobs/<id>/download` | 下载成片 MP4 |
| GET | `/api/avatars` | 数字人列表 |
| GET | `/api/voices` | 音色列表 |
| GET | `/api/avatars/<id>/preview` | 数字人头像预览图 |
| GET | `/api/digital_human_status` | SadTalker 安装状态 |

### 创建任务示例

```bash
curl -X POST http://localhost:7860/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "pptx_path": "C:/path/to/your.pptx",
    "avatar": "teacher_female",
    "voice": "zh-CN-XiaoxiaoNeural",
    "ratio": "16:9",
    "resolution": "720p",
    "digital_human_mode": "auto",
    "enable_subtitle": true,
    "enable_bgm": true
  }'
```

### 删除任务行为

`DELETE /api/jobs/<id>` 会彻底清理：
- `uploads/{stem}.pptx` — 原始上传文件
- `uploads/{stem}.pdf` — PPT 转 PDF 产物
- `uploads/_mineru_{stem}/` — MinerU 解析目录
- `uploads/_slides_{stem}/` — PNG 切片目录
- `output/{job_id}/` — 整个输出工作目录（audio/ scenes/ final/ talking_head_*/）

运行中（`running`/`pending`）任务拒绝删除，返回 409。

## 进阶定制

### 1. 接入真实数字人

**方式 A：SadTalker 对口型（推荐）**
把 SadTalker 仓库克隆到 `keling-batch/sadtalker/`，确保 `sadtalker/inference.py` 存在。系统会自动检测并启用，失败时回退静态头像。

**方式 B：自定义头像视频**
把真实数字人视频放到 `assets/avatars/<your_id>.mp4`，修改 [core.py](backend/core.py) 中 `render_scene_video` 的 `avatar_input` 部分。

### 2. 公式口播转换

LaTeX 公式转中文口播有两套方案，自动切换：

- **DeepSeek LLM**（推荐）：设置 `DEEPSEEK_API_KEY` 后，[core.py:273](backend/core.py#L273) `latex_to_chinese_llm()` 调用 DeepSeek API，带 16 条口播规则 prompt，结果缓存。
- **正则回退**：未配置 key 或调用失败时，[core.py:334](backend/core.py#L334) `latex_to_chinese()` 用正则规则转换，覆盖常见符号、积分、求和、上下标等。

### 3. 添加背景音乐
把任意 mp3 放到 `assets/bgm/default.mp3`，系统会自动以 15% 音量混入。

### 4. PPT 转 PDF 后端选择
- **PowerPoint COM**（优先）：需 Windows + 安装 PowerPoint，速度快（~4 秒）
- **LibreOffice**（兜底）：需安装 soffice 并加入 PATH，处理带公式 PPT 较慢

## 依赖说明

| 包 | 作用 |
|---|---|
| `Flask` | Web API 框架 |
| `python-pptx` | PPTX 文件读取 |
| `pypdfium2` | PDF → PNG 渲染 |
| `mineru[core]>=3.4` | PDF 文字 + 公式识别（异步 API 模式） |
| `edge-tts>=7.2.8` | 微软 Azure TTS（避免旧版 Sec-MS-GEC 403） |
| `imageio-ffmpeg` | 静态 ffmpeg 二进制（不依赖系统 ffmpeg） |
| `pywin32` | Windows PowerPoint COM 调用 |
| `Pillow` | 图像处理 |

**注意**：MinerU 首次运行会从 modelscope 下载模型（约 2GB），需联网。

## 已知问题与兼容补丁

- **pypdfium2 5.x 移除 `PdfImage.get_pos()`**：MinerU 3.4.1 仍依赖此方法，会抛 `AttributeError`。通过 [_mineru_patch/usercustomize.py](_mineru_patch/usercustomize.py) + `PYTHONPATH` 注入子进程补丁解决（见 [core.py:101](backend/core.py#L101) `_ensure_mineru_subprocess_patch`）。
- **LibreOffice 处理带公式 PPT 极慢**：Windows 上 timeout 机制对 soffice.exe 不生效，建议优先用 PowerPoint COM。
- **edge-tts 旧版 403**：必须用 7.2.8 或更新版本，旧版 Sec-MS-GEC token 算法已废弃。

## License
MIT
