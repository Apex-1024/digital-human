# 课灵 AI · 批量制课系统

> 一个本地可运行的"PPT → 数字人讲解视频"批量生成器。
> 把任何 .pptx 拖进去，配上数字人和声音，3 分钟出片，可批量并发。

## 快速开始

### Windows
双击 `start.bat`，等待依赖安装完成，浏览器自动打开 http://localhost:7860

### Linux / macOS
```bash
chmod +x start.sh && ./start.sh
```

## 核心功能

| 步骤 | 做了什么 | 用到的技术 |
|---|---|---|
| ① 解析 PPT | 把每页拆成"标题 + 要点"分镜，渲染为 PNG | python-pptx + LibreOffice |
| ② 数字人形象 | 5 种占位数字人，可换成任意 PNG/SVG | ffmpeg 叠加 + zoompan 微动作 |
| ③ TTS 口播 | 7 种中文 + 1 种英文，免费无限量 | edge-tts（微软 Azure 音色） |
| ④ 视频合成 | PPT 底图 + 数字人头像 + 语音 + BGM | ffmpeg filter_complex |
| ⑤ 批量队列 | 同时跑 2 个任务，状态持久化 | ThreadPoolExecutor + JSON |

## 项目结构

```
keling-batch/
├── backend/
│   ├── app.py          # Flask API（8 个接口）
│   └── core.py         # 核心：解析 / TTS / 合成 / 队列
├── frontend/
│   ├── templates/index.html
│   └── static/
│       ├── style.css
│       └── app.js
├── assets/
│   ├── avatars/        # 数字人头像（自动生成 5 个 SVG 占位）
│   └── bgm/            # 背景音乐（放入 default.mp3 启用）
├── uploads/            # 上传的 PPT 暂存
├── output/             # 生成的视频成片
├── start.bat / start.sh
├── requirements.txt
└── jobs.json           # 任务状态持久化
```

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传 PPT（multipart/form-data） |
| POST | `/api/jobs` | 创建制课任务（JSON body） |
| GET | `/api/jobs` | 列出所有任务 |
| GET | `/api/jobs/<id>` | 查询单个任务 |
| POST | `/api/jobs/<id>/cancel` | 取消运行中的任务 |
| DELETE | `/api/jobs/<id>` | 删除任务及输出 |
| GET | `/api/jobs/<id>/download` | 下载成片 MP4 |
| GET | `/api/avatars` | 数字人列表 |
| GET | `/api/voices` | 音色列表 |
| GET | `/api/avatars/<id>/preview` | 数字人头像预览图 |

## 进阶定制

### 1. 接入真实数字人
把真实数字人视频放到 `assets/avatars/<your_id>.mp4`，修改 `core.py` 中
`render_scene_video` 的 `avatar_input` 部分，把 `-loop 1 -i 图片`
改成 `-i 你的视频.mp4` 即可。

### 2. 接入大模型生成讲解稿
`core.py` 的 `generate_script()` 是启发式拼接。换成你的 LLM 调用即可：

```python
def generate_script(scene: Scene) -> str:
    from openai import OpenAI
    client = OpenAI(api_key="sk-xxx", base_url="https://api.deepseek.com")
    rsp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{
            "role": "user",
            "content": f"请基于以下 PPT 内容写一段 50-100 字的口播稿：\n标题：{scene.title}\n要点：{chr(10).join(scene.bullets)}"
        }]
    )
    return rsp.choices[0].message.content.strip()
```

### 3. 添加背景音乐
把任意 mp3 放到 `assets/bgm/default.mp3`，系统会自动以 15% 音量混入。

### 4. 烧录字幕
字幕功能已预留接口，在 `concatenate_scenes()` 里启用 `subtitle=True` 时
会根据每个分镜的 `script` 字段自动生成 SRT 烧录。

## 性能参考

| PPT 页数 | 720P 渲染耗时 | 1080P 渲染耗时 |
|---|---|---|
| 10 页 | ~ 50 秒 | ~ 80 秒 |
| 20 页 | ~ 90 秒 | ~ 150 秒 |
| 40 页 | ~ 180 秒 | ~ 300 秒 |
| 80 页 | ~ 360 秒 | ~ 600 秒 |

*测试环境：i5-10400 + 16GB，edge-tts 联网 TTS*

## License
MIT
