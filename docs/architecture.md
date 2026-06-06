# 🏗️ Garden AutoCut — 技术架构文档 (v2)

## 1. 总体架构

Garden AutoCut 采用**单体 Flask 应用 + 原生前端 SPA** 的架构，通过 JSON 文件实现数据持久化，无需数据库。

```
┌─────────────────────────────────────────────────────────────────┐
│                         浏览器                                  │
│                   index.html (SPA)                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │项目列表   │ │镜头评分   │ │剧本编辑   │ │发布包     │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│  ┌──────────────────────────────────────────────────┐          │
│  │    SSE EventSource ← 实时进度更新                   │          │
│  └──────────────────────────────────────────────────┘          │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP (REST API + SSE)
┌─────────────────────────▼───────────────────────────────────────┐
│                     Flask Application                           │
│               app/server.py (~2700 行)                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  路由层 (Route Handlers)                                  │   │
│  │  • /api/projects  — 项目 CRUD                            │   │
│  │  • /api/projects/<id>/* — Pipeline 阶段执行               │   │
│  │  • /api/projects/<id>/render — 草稿/发布渲染              │   │
│  │  • /api/projects/<id>/performance — 发布数据记录          │   │
│  │  • /api/performance/report — 周报分析                     │   │
│  │  • /api/video|keyframe|output/<id>/* — 文件服务           │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Pipeline 层 (10 Stages)                                  │   │
│  │  import → keyframes → contact_sheets                      │   │
│  │  → visual_analysis → transcription                        │   │
│  │  → template_select → story_script                         │   │
│  │  → edit_plan → render → publish_pack                      │   │
│  │  → performance                                            │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  校验层 (Pydantic)                                        │   │
│  │  • app/schemas.py — ShotAnalysis, PlatformScores, ...     │   │
│  │  • app/json_utils.py — load_validated, atomic_write_json  │   │
│  │  • AI 输出 → JSON 提取 → Pydantic 校验 → 修正/重试/Fallback│   │
│  └─────────────────────────────────────────────────────────┘   │
└────┬──────────┬──────────┬──────────┬───────────────────────────┘
     │          │          │          │
     ▼          ▼          ▼          ▼
  ffmpeg    Whisper    mimo API    JSON 文件
  ffprobe   (本地)    (多模态)    (持久化存储)
```

## 2. 后端架构

### 2.1 文件组织

```
app/
├── server.py        # Flask 路由 + Pipeline 阶段 + 工具函数 (~2700 行)
├── schemas.py       # Pydantic 数据模型 (205 行)
├── json_utils.py    # JSON 校验加载 + 原子写入 + AI 输出解析 (104 行)
└── templates/
    └── index.html   # 前端 SPA (~1050 行)
```

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

## 3. Pipeline 阶段详细设计 (10 阶段)

### Stage 1: 视频导入 (`stage_import`)

**功能**：扫描 Inbox 目录，将视频按拍摄日期归档

**流程**：
1. 扫描 `Inbox/` 下所有视频文件（.mov, .mp4, .m4v, .avi, .mkv, .hevc, .3gp）
2. 通过 `ffprobe` 读取 `creation_time` 元数据确定拍摄日期
3. `shutil.move()` 移动到 `YYYY-MM-DD/<topic>/raw/`

### Stage 2: 关键帧抽取 (`stage_keyframes`)

**功能**：从每个视频中抽取关键帧

**两种策略**：
- 固定间隔抽帧：每 2 秒一帧 → `keyframes/fixed/`
- 场景变化抽帧：检测画面突变 → `keyframes/scene/`

### Stage 3: Contact Sheet + 时间索引 (`stage_contact_sheets`)

**功能**：将关键帧拼成网格图，叠加时间戳，生成帧索引

**特性**：
- 每帧叠加：视频文件名 + 时间戳(MM:SS.s) + 帧编号
- PingFang 字体，黑底半透明背景
- 输出 `contact_sheets/fixed_sheet.jpg` 和 `scene_sheet.jpg`
- 输出 `frame_index.json`（帧 → 视频/时间映射）

### Stage 4: 片段级视觉分析 (`stage_visual_analysis`)

**功能**：使用多模态 AI 分析关键帧，输出多个候选 shots

**输出**: `shots.json` — 每个视频可能有 1-3 个候选片段

**每个 shot 包含**：
| 字段 | 说明 |
|------|------|
| shot_id | 片段 ID (如 5894_s001) |
| source | 源视频文件名 |
| start/end/duration | 时间范围 |
| visual_summary | 画面描述 |
| shot_types | 类型标签 (全景/中景/特写/整理前/整理后...) |
| garden_objects | 画面中的物体 |
| quality | 画面质量 4 维 (clarity/stability/exposure/composition) |
| platform_scores | 平台评分 8 维 (见下表) |
| recommended_use | 推荐用途 (开头/结尾/封面/中段快切...) |
| delete / delete_reason | 是否建议删除 |

**平台评分 8 维**：
| 字段 | 权重 | 说明 |
|------|------|------|
| hook | 0.18 | 前3秒吸引力 |
| retention | 0.18 | 维持停留能力 |
| action | 0.16 | 动作变化丰富度 |
| story_value | 0.16 | 故事推进价值 |
| beauty | 0.12 | 画面美感 |
| clarity | 0.10 | 一眼看懂程度 |
| contrast | 0.07 | 前后变化对比 |
| cover_value | 0.03 | 封面适合度 |

**AI 输出解析链**：
```
LLM 输出 → extract_json_from_text() → validate_shots() (Pydantic)
→ 失败则重试 1 次 → 仍失败则 fallback
```

### Stage 5: 音频转字幕 (`stage_transcription`)

**功能**：提取音频并用 Whisper 转写为字幕

**Whisper 调用链**：本地 whisper `base` 模型 → OpenAI Whisper API → 空结果

### Stage 5.5: 视频模板选择 (`stage_select_template`)

**功能**：根据 shots 数据自动选择最适合的视频模板

**5 种模板**：
| 模板 | 适用场景 | 判断规则 |
|------|---------|---------|
| before_after | 整理前后对比 | 含整理前 + 核心动作 + 整理后 |
| garden_diary | 花园日记 | action_count ≥ 3 |
| tutorial | 种植/养护教程 | 含花苗/种子/栽种等关键词 |
| healing_mood | 治愈氛围片 | beauty ≥ 8 且 action 少 |
| one_problem | 问题解决型 | 默认 |

**输出**: `video_template.json`

### Stage 6: 剧情脚本生成 (`stage_story_script`)

**功能**：融合视觉分析和字幕转写，生成完整的故事脚本

**改进**：按推荐用途分组构建素材描述（告诉 AI 哪些能做开头、哪些做封面）

### Stage 7: 编辑计划生成 (`stage_edit_plan`)

**功能**：从 shots 生成可渲染的编辑计划，含剪辑意图

**每个 clip 新增字段**：
| 字段 | 说明 |
|------|------|
| why_selected | 为什么选这个 shot |
| platform_goal | 平台目标 (前3秒抓注意力/维持节奏...) |
| risk | 潜在风险 (曝光/抖动/模糊提示) |
| edit_style | 剪辑风格 (normal/fast_cut/slow_motion/fade) |
| speed | 播放速度 |

### Stage 8: 视频渲染 (`stage_render`)

**功能**：根据剪辑计划渲染视频

**两种模式**：
| 模式 | 分辨率 | 预设 | CRF | 用途 |
|------|--------|------|-----|------|
| draft | 720×1280 | ultrafast | 26 | 快速审片，带调试叠加 |
| publish | 1080×1920 | veryfast | 20 | 正式发布 |

**草稿模式调试叠加**：片段编号、角色、时间范围、字幕、时间码

### Stage 9: 发布包生成 (`stage_publish_pack`)

**功能**：生成多平台发布所需的全部素材

**输出**: `publish_pack.json`
| 字段 | 说明 |
|------|------|
| title_candidates | 3 个标题候选 |
| cover_text_candidates | 3 个封面文字候选 |
| description | 发布简介 |
| hashtags | 4-6 个话题标签 |
| comment_prompt | 评论引导问题 |
| platform_notes | 抖音/小红书/视频号各自备注 |

### Stage 10: 发布后反馈 (`stage_record_performance`)

**功能**：记录发布后的平台数据，用于反馈学习

**输出**: `performance.json`
- 按平台记录：播放/点赞/评论/收藏/转发/平均观看/完播率
- 关联模板类型，用于后续分析

**周报分析** (`stage_weekly_report`)：
- 按模板类型统计平均播放/点赞/评论/完播率
- 自动排名 + 生成洞察

## 4. 数据持久化

### 4.1 文件列表

| 文件 | 生成阶段 | 内容 |
|------|---------|------|
| `meta.json` | 创建项目时 | 主题元数据 |
| `shots.json` | Stage 4 | **片段级分析** (新) |
| `analysis.json` | Stage 4 | 向后兼容的扁平化分析 |
| `frame_index.json` | Stage 3 | 关键帧时间索引 (新) |
| `video_template.json` | Stage 5.5 | 视频模板选择 (新) |
| `transcription.json` | Stage 5 | 转写结果 |
| `story_script.json` | Stage 6 | 剧情脚本 |
| `edit_plan.json` | Stage 7 | 编辑计划 (含剪辑意图) |
| `publish_pack.json` | Stage 9 | 发布包 (新) |
| `performance.json` | Stage 10 | 发布后反馈 (新) |
| `outputs/rough_cut.mp4` | Stage 8 | 发布模式渲染 |
| `outputs/draft_cut.mp4` | Stage 8 | 草稿模式渲染 (新) |

### 4.2 Pydantic 校验模型 (app/schemas.py)

所有 AI 生成的 JSON 在写入前必须经过 Pydantic 校验：

```python
from app.schemas import ShotAnalysis, validate_shots
from app.json_utils import atomic_write_json, extract_json_from_text

# AI 输出 → JSON 提取 → Pydantic 校验 → 原子写入
parsed = extract_json_from_text(ai_response)
shots = validate_shots(parsed["shots"])
atomic_write_json("shots.json", {"project_id": pid, "shots": shots})
```

## 5. API 端点一览

### Pipeline 阶段
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/projects/{id}/keyframes` | 关键帧抽取 |
| POST | `/api/projects/{id}/contact-sheets` | Contact Sheet |
| POST | `/api/projects/{id}/visual-analysis` | 片段级视觉分析 |
| POST | `/api/projects/{id}/transcription` | 音频转字幕 |
| POST | `/api/projects/{id}/template-select` | 视频模板选择 |
| POST | `/api/projects/{id}/story-script` | 剧情脚本生成 |
| POST | `/api/projects/{id}/edit-plan` | 编辑计划生成 |
| POST | `/api/projects/{id}/render` | 视频渲染 (draft/publish) |
| POST | `/api/projects/{id}/publish-pack` | 发布包生成 |
| POST | `/api/projects/{id}/performance` | 记录发布数据 |
| POST | `/api/projects/{id}/full-pipeline` | 全流程执行 |

### 数据读写
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/projects` | 项目列表 |
| POST | `/api/projects` | 创建项目 |
| GET | `/api/projects/{id}` | 项目详情 (含 shots/template/publish_pack) |
| PUT | `/api/projects/{id}/story-script` | 保存编辑后的脚本 |
| PUT | `/api/projects/{id}/edit-plan` | 保存编辑后的计划 |

### 分析报告
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/performance/report` | 全局周报分析 |
| GET | `/api/projects/{id}/progress/stream` | SSE 实时进度 |

### 文件服务
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/video/{id}/{filename}` | 原始视频 |
| GET | `/api/keyframe/{id}/{sub}/{filename}` | 关键帧图片 |
| GET | `/api/contact-sheet/{id}/{filename}` | Contact Sheet |
| GET | `/api/output/{id}/{filename}` | 输出文件 |

## 6. AI 模型集成

### 6.1 输出解析链

```
AI 原始输出
  ↓
extract_json_from_text()  — 健壮的括号匹配，不依赖正则
  ↓
Pydantic model 校验      — ShotAnalysis / PlatformScores / ...
  ↓ (校验失败)
字段修正 + 重试           — 兼容旧字段名 (story → story_value)
  ↓ (仍失败)
fallback                 — _fallback_visual_analysis() 生成默认值
```

### 6.2 模型配置

| 用途 | 主模型 | 备选模型 | 温度 | Max Tokens |
|------|--------|---------|------|-----------|
| 视觉分析 | mimo-v2.5 | gpt-4o | 0.2 | 8192 |
| 文本生成 | mimo-v2.5-pro | gpt-4o | 0.7 | 4096 |
| 发布包 | mimo-v2.5-pro | gpt-4o | 0.8 | 2048 |
| 语音识别 | 本地 whisper base | whisper-1 API | - | - |

## 7. 前端架构

### 7.1 标签页

| 标签 | 内容 |
|------|------|
| 📹 素材视频 | 视频预览网格 |
| 🎞️ 关键帧 | 关键帧图片画廊 |
| 🖼️ Contact Sheet | 拼图预览 |
| 👁️ 片段分析 | shot 卡片 + 评分条 + 操作按钮 |
| 🎙️ 字幕转写 | 时间轴 + 转写文本 |
| 📝 剧情脚本 | 可编辑的脚本 + 剪辑意图 |
| 📋 剪辑计划 | 可编辑的计划 + 剪辑意图 |
| 📦 发布包 | 标题/封面/话题/平台备注 + 数据记录 |
| 🎬 输出 | 渲染视频预览 |

### 7.2 镜头评分面板操作

每个 shot 卡片提供：
- 🎯 设为开头 — 替换编辑计划的 opening 片段
- 🖼️ 设为封面 — 替换 result 片段
- ▶ 预览 — 弹窗预览视频片段
- 🗑️ 标记删除 — 标记为不使用

## 8. 故事角色系统 (STORY_SLOTS)

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

## 9. 安全与可靠性设计

### 9.1 路径安全

所有文件服务 API 共用集中路径校验：

```python
def resolve_project_dir(project_id: str) -> str:
    """normpath + startswith 双重校验，防止 ../ 路径穿越"""

def resolve_file_path(project_id: str, sub_dir: str, filename: str) -> str:
    """文件名不能含路径分隔符，最终路径必须在允许的子目录下"""
```

覆盖的端点：`/api/video`、`/api/keyframe`、`/api/contact-sheet`、`/api/output`、`/api/file`

### 9.2 并发控制

项目级锁防止同一项目并发执行 pipeline 或渲染：

```python
project_locks: dict[str, threading.Lock] = {}

# full-pipeline 和 render 端点使用
if not acquire_project_lock(project_id):
    return error_response("PIPELINE_RUNNING", "...", status=409)
```

- 冲突返回 `409 Conflict`
- `finally` 块确保释放锁
- `job_status.json` 落盘，服务重启后可查

### 9.3 统一错误响应

```json
{
  "error": {
    "code": "PIPELINE_RUNNING",
    "message": "项目 xxx 正在处理中",
    "stage": "pipeline",
    "retryable": false
  }
}
```

| HTTP 状态码 | 场景 |
|------------|------|
| 400 | 参数错误 / 非法 project_id |
| 404 | 项目不存在 / 文件不存在 |
| 409 | 项目正在处理中 |
| 422 | AI 输出校验失败 |
| 403 | 路径越界 |
| 500 | 内部错误 |

### 9.4 渲染 concat 校验

concat 前对所有临时片段跑 `ffprobe -show_streams` 校验一致性：
- width / height / r_frame_rate / codec_name / pix_fmt / sample_rate
- 不一致的片段自动重新转码后再 concat

### 9.5 其他安全措施

- **XSS 防护**：前端 `esc()` 函数转义 HTML 特殊字符
- **原子写入**：`atomic_write_json()` 先写临时文件再 rename
- **超时保护**：subprocess 调用默认 600 秒超时，ffprobe 10 秒超时
