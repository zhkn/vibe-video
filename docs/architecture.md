# 🏗️ Garden AutoCut — 技术架构文档

## 1. 总体架构

Garden AutoCut 采用**单体 Flask 应用 + 原生前端 SPA** 的架构，通过 JSON 文件实现数据持久化，无需数据库。

```
┌─────────────────────────────────────────────────────────────────┐
│                         浏览器                                  │
│                   index.html (SPA)                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │项目列表   │ │视频预览   │ │剧本编辑   │ │渲染输出   │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│  ┌──────────────────────────────────────────────────┐          │
│  │    SSE EventSource ← 实时进度更新                   │          │
│  └──────────────────────────────────────────────────┘          │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP (REST API + SSE)
┌─────────────────────────▼───────────────────────────────────────┐
│                     Flask Application                           │
│                   app.py (~1774 行)                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  路由层 (Route Handlers)                                  │   │
│  │  • /api/projects  — 项目 CRUD                            │   │
│  │  • /api/projects/<id>/*-pipeline — Pipeline 阶段执行       │   │
│  │  • /api/projects/<id>/story-script (PUT) — 脚本编辑保存    │   │
│  │  • /api/projects/<id>/edit-plan (PUT) — 计划编辑保存       │   │
│  │  • /api/projects/<id>/progress/stream — SSE 进度推送       │   │
│  │  • /api/video|keyframe|output/<id>/* — 文件服务            │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Pipeline 层 (8 Stages)                                   │   │
│  │  stage_import → stage_keyframes → stage_contact_sheets    │   │
│  │  → stage_visual_analysis → stage_transcription            │   │
│  │  → stage_story_script → stage_edit_plan → stage_render    │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  工具层                                                   │   │
│  │  • get_video_info() — ffprobe 视频信息（带缓存）           │   │
│  │  • run_cmd() — subprocess 封装                            │   │
│  │  • update_progress() — 线程安全进度更新                    │   │
│  └─────────────────────────────────────────────────────────┘   │
└────┬──────────┬──────────┬──────────┬───────────────────────────┘
     │          │          │          │
     ▼          ▼          ▼          ▼
  ffmpeg    Whisper    mimo API    JSON 文件
  ffprobe   (本地)    (多模态)    (持久化存储)
```

## 2. 后端架构

### 2.1 Flask 应用

- **框架**：Flask，使用 `app.run(threaded=True)` 支持并发请求
- **模板**：Jinja2 模板引擎，仅用于渲染 `index.html`
- **启动参数**：`--host`（默认 127.0.0.1）、`--port`（默认 8766）、`--debug`
- **路径配置**：
  - `ROOT_DIR` = `~/Movies/GardenAutoCut`（项目根目录）
  - `INBOX_DIR` = `ROOT_DIR/Inbox`（收件箱）
  - `APP_DIR` = 脚本所在目录（`app/`）

### 2.2 线程模型

```
主线程 (Flask)
  │
  ├── GET 请求 → 同步处理
  ├── POST /api/.../full-pipeline → 启动后台线程 → 立即返回
  │     └── daemon 线程 → run_full_pipeline() → 逐阶段执行
  │           └── update_progress() → progress_store (dict + lock)
  └── GET /api/.../progress/stream → SSE 生成器 → 轮询 progress_store
```

- **并发模型**：Flask threaded 模式，每个请求独立线程
- **全流程异步**：`full-pipeline` 端点启动 daemon 线程，立即返回 `{"status": "started"}`
- **单阶段同步**：其他 Pipeline 端点在请求线程中同步执行
- **进度共享**：全局 `progress_store` 字典 + `threading.Lock` 保护

### 2.3 进度追踪系统

```python
progress_store: dict[str, dict] = {}  # project_id → progress data
progress_lock = threading.Lock()

def update_progress(project_id, stage, percent, message, detail=""):
    with progress_lock:
        progress_store[project_id] = {
            "stage": stage,       # 当前阶段名
            "percent": percent,   # 0-100
            "message": message,   # 状态消息
            "detail": detail,     # 附加信息
            "timestamp": time.time(),
        }
```

SSE 流每 0.5 秒检查一次，仅在数据变化时发送，`percent >= 100` 或 `stage == "error"` 时自动结束流。

### 2.4 SSE (Server-Sent Events)

```python
@app.route("/api/projects/<project_id>/progress/stream")
def api_progress_stream(project_id):
    def generate():
        last_data = None
        while True:
            data = progress_store.get(project_id, {...})
            if data != last_data:
                yield f"data: {json.dumps(data)}\n\n"
                last_data = data
                if data["percent"] >= 100 or data["stage"] == "error":
                    break
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")
```

前端通过 `EventSource` API 接收：
```javascript
progressSSE = new EventSource(`/api/projects/${id}/progress/stream`);
progressSSE.onmessage = (e) => {
    const data = JSON.parse(e.data);
    updateProgressUI(data);
};
```

## 3. 前端架构

### 3.1 设计特点

- **单文件 SPA**：全部代码在 `index.html` 中（HTML + CSS + JS，~820 行）
- **暗色主题**：GitHub 风格的 CSS 变量系统
- **无框架**：原生 JavaScript，无 React/Vue 依赖
- **响应式**：CSS Grid + Media Query，支持移动端

### 3.2 CSS 变量系统

```css
:root {
    --bg: #0d1117;       /* 主背景 */
    --bg2: #161b22;      /* 卡片背景 */
    --bg3: #21262d;      /* 输入框背景 */
    --border: #30363d;   /* 边框 */
    --text: #e6edf3;     /* 主文字 */
    --text2: #8b949e;    /* 次要文字 */
    --accent: #58a6ff;   /* 主强调色 (蓝) */
    --accent2: #3fb950;  /* 成功色 (绿) */
    --accent3: #d2a8ff;  /* 辅助色 (紫) */
    --warn: #d29922;     /* 警告色 */
    --danger: #f85149;   /* 危险色 */
}
```

### 3.3 页面结构

```
┌──────────────────────────────────────┐
│ Header: ← 返回 | 🌿 Garden AutoCut  │
├──────────────────────────────────────┤
│ 项目列表视图 (默认显示)               │
│  ┌────────┐ ┌────────┐ ┌────────┐   │
│  │ 项目卡片│ │ 项目卡片│ │ 项目卡片│   │
│  └────────┘ └────────┘ └────────┘   │
├──────────────────────────────────────┤
│ 项目详情视图 (点击项目后显示)          │
│  ┌─────────────────────────────┐     │
│  │ 控制栏: 主题输入 | 时长选择   │     │
│  │ 按钮: 全流程 | 抽帧 | 分析... │     │
│  │ 进度条: ████████░░ 80%       │     │
│  └─────────────────────────────┘     │
│  ┌─────────────────────────────┐     │
│  │ 标签栏: 素材|关键帧|分析|...   │     │
│  ├─────────────────────────────┤     │
│  │ 标签内容区                    │     │
│  └─────────────────────────────┘     │
└──────────────────────────────────────┘
```

### 3.4 核心交互

| 操作 | 实现方式 |
|------|---------|
| 项目列表 | `fetch('/api/projects')` → DOM 渲染 |
| 标签切换 | CSS 类切换 (`active`)，无路由 |
| 运行阶段 | `POST /api/.../stage` → SSE 监听 → 刷新项目数据 |
| 全流程 | `POST /api/.../full-pipeline` → SSE 监听 + 轮询完成 |
| 编辑剧本 | DOM 读取 `input`/`select` 值 → `PUT /api/.../story-script` |
| 片段预览 | `<video src="url#t=start,end">` 原生时间片段 |
| 图片放大 | Lightbox 全屏覆盖层 |

## 4. Pipeline 阶段详细设计

### Stage 1: 视频导入 (`stage_import`)

**功能**：扫描 Inbox 目录，将视频按拍摄日期归档

**流程**：
1. 扫描 `Inbox/` 下所有视频文件（.mov, .mp4, .m4v, .avi, .mkv, .hevc, .3gp）
2. 通过 `ffprobe` 读取 `creation_time` 元数据确定拍摄日期
3. 如无元数据，使用文件修改时间
4. `shutil.move()` 移动到 `YYYY-MM-DD/raw/`

**变体**：`stage_import_direct()` 支持直接导入指定文件列表（用于 API 上传）

### Stage 2: 关键帧抽取 (`stage_keyframes`)

**功能**：从每个视频中抽取关键帧

**两种策略**：

```bash
# 固定间隔抽帧 — 每 2 秒一帧
ffmpeg -y -i video.mp4 -vf "fps=1/2" output_%04d.jpg

# 场景变化抽帧 — 检测画面突变
ffmpeg -y -i video.mp4 -vf "select='gt(scene,0.35)'" -vsync vfr output_%04d.jpg
```

**输出**：`keyframes/fixed/` 和 `keyframes/scene/` 两个子目录

### Stage 3: Contact Sheet (`stage_contact_sheets`)

**功能**：将关键帧拼成网格图，便于多模态模型一次读取

**实现**：
- 使用 Pillow 库
- 每种类型最多取 30 帧（均匀采样）
- 缩略图尺寸：324×576 (9:16 比例)
- 最多 5 列，自动计算行数
- 每帧左上角标注编号 `#1`, `#2`, ...
- 输出：`contact_sheets/fixed_sheet.jpg` 和 `scene_sheet.jpg`

### Stage 4: 视觉分析 (`stage_visual_analysis`)

**功能**：使用多模态 AI 模型分析关键帧内容

**Prompt 设计**：
- 输入：Contact Sheet 图片（base64 编码）+ 视频文件信息列表 + 用户主题偏好
- 输出要求：每个视频一个 JSON 对象，包含：
  - `file`：源视频文件名
  - `start`/`end`/`duration`：建议片段时间范围
  - `action`：画面动作描述（15 字以内）
  - `subjects`：画面元素列表
  - `story_role`：故事角色（9 选 1）
  - `quality_score`/`stability`/`privacy_risk`：1-10 评分
  - `caption`：字幕文字（15 字以内）

**API 调用链**：
```
优先: XIAOMI_API_KEY + XIAOMI_BASE_URL → mimo-v2.5
备选: OPENAI_API_KEY → gpt-4o
失败: _fallback_visual_analysis() — 基于文件信息生成默认分析
```

### Stage 5: 音频转字幕 (`stage_transcription`)

**功能**：提取视频音频并用 Whisper 转写为字幕

**流程**：
1. `ffmpeg` 提取音频为 WAV（16kHz, 单声道, PCM 16bit）
2. Whisper 转写（优先本地 `base` 模型，fallback 到 API）
3. 生成 `transcription.json` 和 `raw_captions.srt`

**Whisper 调用链**：
```
优先: 本地 whisper.load_model("base") → 转写
备选: OpenAI Whisper API (whisper-1)
失败: 返回空结果
```

### Stage 6: 剧情脚本生成 (`stage_story_script`)

**功能**：融合视觉分析和字幕转写，生成完整的故事脚本

**Prompt 要点**：
- 角色：短视频编剧，60 秒竖屏生活记录
- 输入：视觉分析结果 + 音频转写文本 + 可用视频列表
- 要求：故事结构（开场→展开→高潮→收尾），字幕简短有力
- 输出 JSON 含：title, subtitle, storyline, tone, clips[], hashtags[]

**源文件校正**：
- AI 可能返回错误的文件名（如 frame_set_X.jpg）
- 自动通过前缀模糊匹配修正为实际视频文件名

**Fallback**：
- 无 API 时生成基础脚本，按 STORY_SLOTS 顺序分配视频

### Stage 7: 编辑计划生成 (`stage_edit_plan`)

**功能**：从故事脚本生成可渲染的编辑计划

**两种模式**：

1. **有故事脚本**：直接转换，补充 `source_path`、`source_duration`、`timeline_start/end`
2. **无故事脚本**（仅从分析结果）：
   - 按质量评分加权排序（质量 30% + 内容 25% + 角色匹配 20% + 稳定性 10% + 花园关键词 10%）
   - 按 STORY_SLOTS 角色选最佳片段
   - 隐私风险 ≥5 的片段扣分

### Stage 8: 视频渲染 (`stage_render`)

**功能**：根据剪辑计划渲染最终视频

**渲染流程**：

```
1. 逐片段裁剪 + 缩放
   ffmpeg -ss START -t DURATION -i SOURCE \
     -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,format=yuv420p" \
     -c:v libx264 -preset veryfast -crf 20 \
     -c:a aac -ar 44100 -ac 2 \
     -movflags +faststart \
     TEMP_CLIP.mp4

2. 片段合并
   ffmpeg -f concat -safe 0 -i _concat.txt -c copy \
     -movflags +faststart rough_cut.mp4

3. 字幕烧录
   ffmpeg -i rough_cut.mp4 \
     -vf "subtitles='captions.srt':force_style='FontName=PingFang SC,FontSize=14,Alignment=2,MarginV=120,Outline=2'" \
     -c:v libx264 -preset veryfast -crf 20 -c:a copy \
     -movflags +faststart final_with_subtitles.mp4

4. 封面生成
   ffmpeg -ss MID_TIME -i rough_cut.mp4 -vframes 1 \
     -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" \
     cover.jpg

5. 清理临时文件
```

**音频处理**：
- 有音频的源视频：直接保留原始音频
- 无音频的源视频：生成静音轨道（stereo, 44100Hz）

## 5. AI 模型集成

### 5.1 视觉分析模型

| 项目 | 值 |
|------|-----|
| **主模型** | mimo-v2.5（Xiaomi API） |
| **备选模型** | gpt-4o（OpenAI API） |
| **输入** | Contact Sheet 图片 (base64) + 文本 Prompt |
| **温度** | 0.2（低温度，追求确定性输出） |
| **最大 Token** | 4096 |
| **输出解析** | 正则提取 JSON 数组/对象 |

### 5.2 文本生成模型

| 项目 | 值 |
|------|-----|
| **主模型** | mimo-v2.5-pro（Xiaomi API） |
| **备选模型** | gpt-4o（OpenAI API） |
| **输入** | 视觉分析 + 转写文本 + 视频列表 + 用户主题 |
| **温度** | 0.7（适度创造性的剧本生成） |
| **最大 Token** | 4096 |
| **输出解析** | 正则提取 JSON 对象 |

### 5.3 语音识别

| 项目 | 值 |
|------|-----|
| **优先方式** | 本地 Whisper `base` 模型 |
| **备选方式** | OpenAI Whisper API (`whisper-1`) |
| **语言** | 中文 (zh) |
| **输入格式** | WAV (16kHz, mono, PCM 16bit) |
| **输出** | segments[] 含 start, end, text |

## 6. 数据持久化

所有数据以 JSON 文件存储在项目目录中，无数据库依赖。

### 6.1 文件列表

| 文件 | 生成阶段 | 内容 |
|------|---------|------|
| `analysis.json` | Stage 4 | 视觉分析结果数组 |
| `transcription.json` | Stage 5 | 每个视频的转写结果数组 |
| `raw_captions.srt` | Stage 5 | 全部视频的合并 SRT 字幕 |
| `story_script.json` | Stage 6 | 完整剧情脚本 |
| `edit_plan.json` | Stage 7 | 可渲染的编辑计划 |
| `outputs/render_info.json` | Stage 8 | 渲染元信息 |

### 6.2 核心数据结构

**analysis.json**:
```json
[{
    "file": "5894_raw.MP4",
    "start": 0.0,
    "end": 15.0,
    "duration": 15.0,
    "action": "修剪枝叶",
    "subjects": ["花园", "剪刀", "绿植"],
    "story_role": "action",
    "quality_score": 8,
    "stability": 9,
    "privacy_risk": 1,
    "caption": "修剪夏日的枝桠"
}]
```

**story_script.json**:
```json
{
    "title": "夏日花园的断舍离",
    "subtitle": "给花园一次新生",
    "storyline": "从杂乱到整洁的花园改造之旅",
    "tone": "清新治愈",
    "duration_target": 60,
    "music_style": "轻音乐",
    "voiceover": false,
    "clips": [{
        "id": "001_opening",
        "role": "opening",
        "source": "5894_raw.MP4",
        "start": 0.0,
        "end": 5.0,
        "duration": 5.0,
        "caption": "阳光洒满花园",
        "voiceover_text": "",
        "transition": "cut",
        "note": "开场空镜"
    }],
    "total_duration": 66,
    "ending_caption": "今天就到这里",
    "hashtags": ["花园", "生活记录"]
}
```

**edit_plan.json**:
```json
{
    "version": "story-script-v1",
    "created_at": "2026-06-05T10:00:00",
    "title": "夏日花园的断舍离",
    "target_duration": 66,
    "actual_duration": 66.5,
    "render": { "width": 1080, "height": 1920, ... },
    "clips": [{
        "role": "opening",
        "source": "5894_raw.MP4",
        "source_path": "/path/to/raw/5894_raw.MP4",
        "source_duration": 120.5,
        "source_has_audio": true,
        "start": 0.0,
        "end": 5.0,
        "duration": 5.0,
        "timeline_start": 0.0,
        "timeline_end": 5.0,
        "caption": "阳光洒满花园",
        "note": "开场空镜"
    }]
}
```

### 6.3 视频信息缓存

```python
_video_info_cache: dict[str, dict] = {}  # path → info
```

`get_video_info()` 对 ffprobe 结果进行内存缓存，避免同一文件重复调用。

## 7. 故事角色系统 (STORY_SLOTS)

系统定义了 9 种故事角色，用于组织视频叙事结构：

| 顺序 | 角色 | 标签 | 说明 |
|------|------|------|------|
| 1 | `opening` | 开场空镜 | 吸引注意力的美丽画面 |
| 2 | `space` | 环境交代 | 展示整体空间 |
| 3 | `action_intro` | 动作引入 | 即将开始的劳动 |
| 4 | `action` | 核心动作 | 主要劳动过程 |
| 5 | `collect` | 收集整理 | 成果收集 |
| 6 | `life` | 生活气息 | 背影、细节、自然感 |
| 7 | `result` | 成果展示 | 劳动成果 |
| 8 | `detail` | 细节特写 | 精致细节 |
| 9 | `ending` | 收尾空镜 | 留有余韵的结束 |

每个视频片段被 AI 分析后会分配一个角色，编辑计划按此角色顺序排列片段。

## 8. 安全设计

- **文件访问控制**：`/api/file/<path>` 端点检查路径必须在 `ROOT_DIR` 下
- **XSS 防护**：前端 `esc()` 函数转义 HTML 特殊字符
- **路径注入**：使用 `os.path.abspath()` + `startswith()` 校验
- **超时保护**：subprocess 调用默认 600 秒超时，ffprobe 10 秒超时
