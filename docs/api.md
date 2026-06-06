# 🔌 Garden AutoCut — API 接口参考

> Base URL: `http://127.0.0.1:8766`

## 目录

- [项目管理](#项目管理)
- [Pipeline 阶段执行](#pipeline-阶段执行)
- [编辑保存](#编辑保存)
- [实时进度](#实时进度)
- [文件服务](#文件服务)
- [SSE 进度流格式](#sse-进度流格式)

---

## 项目管理

### 列出所有项目

```
GET /api/projects
```

**响应示例**：
```json
[
    {
        "id": "2026-06-05",
        "date": "2026-06-05",
        "video_count": 7,
        "has_output": true,
        "has_analysis": true,
        "has_edit_plan": true,
        "has_story_script": true
    },
    {
        "id": "2026-06-04",
        "date": "2026-06-04",
        "video_count": 3,
        "has_output": false,
        "has_analysis": false,
        "has_edit_plan": false,
        "has_story_script": false
    }
]
```

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 项目 ID（即日期字符串） |
| `date` | string | 日期 |
| `video_count` | number | 原始视频数量 |
| `has_output` | boolean | 是否有渲染输出 |
| `has_analysis` | boolean | 是否有视觉分析结果 |
| `has_edit_plan` | boolean | 是否有编辑计划 |
| `has_story_script` | boolean | 是否有剧情脚本 |

---

### 获取项目详情

```
GET /api/projects/{project_id}
```

**路径参数**：

| 参数 | 说明 |
|------|------|
| `project_id` | 项目 ID（如 `2026-06-05`） |

**响应示例**：
```json
{
    "id": "2026-06-05",
    "date": "2026-06-05",
    "dir": "/Users/zhkn/Movies/GardenAutoCut/2026-06-05",
    "videos": [
        {
            "filename": "5894_raw.MP4",
            "path": "/Users/zhkn/Movies/GardenAutoCut/2026-06-05/raw/5894_raw.MP4",
            "duration": 120.5,
            "size_mb": 95.3,
            "width": 1920,
            "height": 1080,
            "fps": 29.97,
            "has_audio": true,
            "creation_time": "2026-06-05T10:30:00Z"
        }
    ],
    "keyframes": {
        "fixed": ["5894_raw_0001.jpg", "5894_raw_0002.jpg"],
        "scene": ["5894_raw_0001.jpg"]
    },
    "analysis": [ ... ],
    "transcription": [ ... ],
    "story_script": { ... },
    "edit_plan": { ... },
    "outputs": {
        "rough_cut.mp4": { "size_mb": 44.0, "path": "..." },
        "cover.jpg": { "size_mb": 0.3, "path": "..." },
        "captions.srt": { "size_mb": 0.01, "path": "..." }
    }
}
```

---

## Pipeline 阶段执行

### 导入视频

```
POST /api/projects/{project_id}/import
Content-Type: application/json
```

**请求体**：无（或空 JSON `{}`）

**响应**：
```json
{
    "imported": [
        { "file": "video1.MOV", "date": "2026-06-05" }
    ]
}
```

---

### 抽关键帧

```
POST /api/projects/{project_id}/keyframes
Content-Type: application/json
```

**请求体**：
```json
{
    "fps_interval": 2.0,
    "scene_threshold": 0.35
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `fps_interval` | float | 2.0 | 固定抽帧间隔（秒） |
| `scene_threshold` | float | 0.35 | 场景变化阈值 (0-1) |

**响应**：
```json
{
    "results": [
        { "file": "5894_raw.MP4", "fixed_frames": 60, "scene_frames": 12 }
    ]
}
```

---

### 生成 Contact Sheet

```
POST /api/projects/{project_id}/contact-sheets
Content-Type: application/json
```

**请求体**：无

**响应**：
```json
{
    "sheets": [
        { "type": "fixed", "path": "...", "frame_count": 30 },
        { "type": "scene", "path": "...", "frame_count": 12 }
    ]
}
```

---

### 视觉分析

```
POST /api/projects/{project_id}/visual-analysis
Content-Type: application/json
```

**请求体**：
```json
{
    "theme": "花园修剪整理"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `theme` | string | "日常生活记录" | 用户主题偏好 |

**响应**：
```json
{
    "analysis": [
        {
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
        }
    ],
    "count": 1
}
```

---

### 音频转字幕

```
POST /api/projects/{project_id}/transcription
Content-Type: application/json
```

**请求体**：无

**响应**：
```json
{
    "transcriptions": [
        {
            "file": "5894_raw.MP4",
            "language": "zh",
            "segments": [
                { "start": 0.0, "end": 3.5, "text": "今天来修剪一下花园" }
            ],
            "text": "今天来修剪一下花园 ..."
        }
    ],
    "segment_count": 92
}
```

---

### 生成剧情脚本

```
POST /api/projects/{project_id}/story-script
Content-Type: application/json
```

**请求体**：
```json
{
    "theme": "花园修剪整理",
    "duration": 60
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `theme` | string | "日常生活记录" | 视频主题 |
| `duration` | int | 60 | 目标时长（秒） |

**响应**：
```json
{
    "script": {
        "title": "夏日花园的断舍离",
        "subtitle": "给花园一次新生",
        "storyline": "从杂乱到整洁的花园改造之旅",
        "tone": "清新治愈",
        "duration_target": 60,
        "music_style": "轻音乐",
        "voiceover": false,
        "clips": [ ... ],
        "total_duration": 66,
        "ending_caption": "今天就到这里",
        "hashtags": ["花园", "生活记录"]
    }
}
```

---

### 生成编辑计划

```
POST /api/projects/{project_id}/edit-plan
Content-Type: application/json
```

**请求体**：
```json
{
    "theme": "花园修剪整理"
}
```

**响应**：
```json
{
    "plan": {
        "version": "story-script-v1",
        "created_at": "2026-06-05T10:00:00",
        "title": "夏日花园的断舍离",
        "target_duration": 66,
        "actual_duration": 66.5,
        "render": { "width": 1080, "height": 1920, "fps": 30, ... },
        "clips": [
            {
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
            }
        ]
    }
}
```

---

### 渲染视频

```
POST /api/projects/{project_id}/render
Content-Type: application/json
```

**请求体**：
```json
{
    "burn_subtitles": true,
    "audio_mode": "source"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `burn_subtitles` | boolean | true | 是否烧录字幕到视频 |
| `audio_mode` | string | "source" | 音频模式：`"source"` 保留原声，其他值生成静音 |

**响应**：
```json
{
    "output": "/path/to/final_with_subtitles.mp4",
    "rough_cut": "/path/to/rough_cut.mp4",
    "srt": "/path/to/captions.srt",
    "cover": "/path/to/cover.jpg",
    "title": "夏日花园的断舍离",
    "duration": 66.5,
    "clip_count": 8,
    "subtitle_count": 8,
    "rendered_at": "2026-06-05T10:05:00.123456"
}
```

---

### 一键全流程

```
POST /api/projects/{project_id}/full-pipeline
Content-Type: application/json
```

**请求体**：
```json
{
    "theme": "花园修剪整理",
    "duration": 60
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `theme` | string | "日常生活记录" | 视频主题 |
| `duration` | int | 60 | 目标时长（秒） |

**响应**（立即返回）：
```json
{
    "status": "started",
    "project_id": "2026-06-05",
    "theme": "花园修剪整理"
}
```

> **注意**：此端点在后台线程中运行，立即返回。通过 SSE 进度流或轮询进度接口获取执行状态。

---

## 编辑保存

### 保存剧情脚本

```
PUT /api/projects/{project_id}/story-script
Content-Type: application/json
```

**请求体**：完整的 story_script.json 内容

**响应**：
```json
{
    "status": "saved",
    "script_path": "/path/to/story_script.json",
    "plan_clips": 8,
    "plan_duration": 66.5,
    "plan": { ... }
}
```

> **注意**：保存脚本时会**自动同步**生成 `edit_plan.json`，响应中包含同步后的编辑计划。

---

### 保存编辑计划

```
PUT /api/projects/{project_id}/edit-plan
Content-Type: application/json
```

**请求体**：完整的 edit_plan.json 内容

**响应**：
```json
{
    "status": "saved",
    "plan_path": "/path/to/edit_plan.json",
    "clips": 8,
    "duration": 66.5
}
```

> **注意**：保存时会自动重新计算 `timeline_start` 和 `timeline_end`。

---

## 实时进度

### 获取当前进度（轮询）

```
GET /api/projects/{project_id}/progress
```

**响应**：
```json
{
    "stage": "visual_analysis",
    "percent": 45,
    "message": "调用 mimo-v2.5 分析画面...",
    "detail": "",
    "timestamp": 1717612345.123
}
```

### SSE 实时进度流

```
GET /api/projects/{project_id}/progress/stream
Accept: text/event-stream
```

**SSE 数据格式**：

```
data: {"stage":"keyframes","percent":15,"message":"抽帧: 5894_raw.MP4","detail":"","timestamp":1717612345.123}

data: {"stage":"keyframes","percent":50,"message":"抽帧: 5902_raw.MP4","detail":"","timestamp":1717612346.456}

data: {"stage":"pipeline","percent":100,"message":"✅ 全流程完成!","detail":"","timestamp":1717612800.789}
```

**进度数据字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `stage` | string | 当前阶段名 |
| `percent` | int | 进度百分比 (0-100, -1 表示错误) |
| `message` | string | 状态消息 |
| `detail` | string | 附加信息 |
| `timestamp` | float | Unix 时间戳 |

**阶段名称对照**：

| stage 值 | 含义 |
|----------|------|
| `idle` | 空闲 |
| `pipeline` | 全流程执行中 |
| `import` | 导入视频 |
| `keyframes` | 抽关键帧 |
| `contact_sheets` | 生成 Contact Sheet |
| `visual_analysis` | 视觉分析 |
| `transcription` | 音频转字幕 |
| `story_script` | 生成剧情脚本 |
| `edit_plan` | 生成编辑计划 |
| `render` | 渲染视频 |
| `error` | 错误 |

**SSE 流生命周期**：
- 流在 `percent >= 100` 或 `stage == "error"` 时自动结束
- 每 0.5 秒检查一次进度，仅在数据变化时发送
- 前端 `EventSource` 断开后自动清理

---

## 文件服务

### 原始视频

```
GET /api/video/{project_id}/{filename}
```

返回 `raw/` 目录下的视频文件，支持浏览器原生 `<video>` 标签播放和 Range 请求。

### 关键帧图片

```
GET /api/keyframe/{project_id}/{sub}/{filename}
```

| 参数 | 说明 |
|------|------|
| `sub` | 子目录：`fixed` 或 `scene` |

### Contact Sheet

```
GET /api/contact-sheet/{project_id}/{filename}
```

### 输出文件

```
GET /api/output/{project_id}/{filename}
```

返回 `outputs/` 目录下的文件（视频、封面、字幕等）。

### 通用文件服务

```
GET /api/file/{filepath}
```

**安全限制**：文件路径必须在 `ROOT_DIR` (`~/Movies/GardenAutoCut/`) 下，否则返回 403。

---

## 错误响应格式

所有端点在出错时返回统一的 JSON 格式：

```json
{
    "error": "错误描述信息"
}
```

**常见 HTTP 状态码**：

| 状态码 | 说明 |
|--------|------|
| 200 | 成功（即使内部出错也可能返回 200 + error 字段） |
| 400 | 请求体为空 |
| 403 | 文件访问被拒绝 |
| 404 | 项目不存在 |

---

## 调用示例

### cURL

```bash
# 列出项目
curl http://127.0.0.1:8766/api/projects

# 获取项目详情
curl http://127.0.0.1:8766/api/projects/2026-06-05

# 运行单个阶段
curl -X POST http://127.0.0.1:8766/api/projects/2026-06-05/keyframes

# 带参数运行
curl -X POST http://127.0.0.1:8766/api/projects/2026-06-05/visual-analysis \
  -H "Content-Type: application/json" \
  -d '{"theme": "花园修剪整理"}'

# 一键全流程
curl -X POST http://127.0.0.1:8766/api/projects/2026-06-05/full-pipeline \
  -H "Content-Type: application/json" \
  -d '{"theme": "花园修剪整理", "duration": 60}'

# 查看进度
curl http://127.0.0.1:8766/api/projects/2026-06-05/progress

# 保存编辑后的脚本
curl -X PUT http://127.0.0.1:8766/api/projects/2026-06-05/story-script \
  -H "Content-Type: application/json" \
  -d '{"title": "新标题", "clips": [...]}'
```

### JavaScript (浏览器)

```javascript
// 运行全流程并监听进度
async function runPipeline(projectId, theme) {
    // 启动 SSE
    const es = new EventSource(`/api/projects/${projectId}/progress/stream`);
    es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        console.log(`[${data.stage}] ${data.message} (${data.percent}%)`);
        if (data.percent >= 100) es.close();
    };

    // 启动全流程
    await fetch(`/api/projects/${projectId}/full-pipeline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme, duration: 60 })
    });
}
```

### Python

```python
import requests

BASE = "http://127.0.0.1:8766"

# 列出项目
projects = requests.get(f"{BASE}/api/projects").json()

# 运行视觉分析
result = requests.post(
    f"{BASE}/api/projects/2026-06-05/visual-analysis",
    json={"theme": "花园修剪整理"}
).json()
print(f"分析了 {result['count']} 个片段")
```
