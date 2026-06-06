# 🌿 Vibe Video — AI 剪辑导演

> 当前版本：**v3.0** — 从"视频剪辑器"进化为"短视频发布助理"

iPhone 拍摄 → 片段级理解 → 智能编排 → 一键生成可发布短视频

## ✨ v3.0 核心能力

| 能力 | 说明 |
|------|------|
| **片段级素材理解** | 每个视频切出多个候选 shots，不是"一个视频一个结论" |
| **8 维平台评分** | hook / retention / action / beauty / clarity / contrast / story_value / cover_value |
| **自动模板选择** | 整理前后对比 / 花园日记 / 教程 / 治愈片 / 问题解决型 |
| **可解释剪辑决策** | edit_plan 含 why_selected / platform_goal / risk / alternatives |
| **发布包生成** | 标题候选 / 封面文字 / 话题标签 / 评论引导 / 平台备注 |
| **双模式渲染** | 草稿 (720p 快速审片) / 发布 (1080p 正式版) |
| **反馈闭环** | 记录发布数据，按模板类型分析周报，持续优化 |
| **镜头评分面板** | UI 中直接设为开头/封面/预览/删除，比编辑 JSON 快 10 倍 |

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     浏览器 Web UI                            │
│    素材 · 片段分析 · 镜头评分 · 剧本 · 剪辑计划 · 发布包 · 输出  │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP / SSE
┌──────────────────────────▼──────────────────────────────────┐
│              Flask 后端 (server.py ~2700 行)                  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 10 阶段 Pipeline                                       │  │
│  │ 导入 → 抽帧 → 拼图+索引 → 片段分析 → 转写              │  │
│  │ → 模板选择 → 剧本 → 剪辑计划 → 渲染 → 发布包 → 反馈    │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Pydantic 校验层 (schemas.py)                           │  │
│  │ ShotAnalysis · PlatformScores · EditPlan · PublishPack │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 安全层: 路径校验 · 项目级锁 · 统一错误码 · 原子写入     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────┬──────────┬──────────┬──────────┬──────────────────────┘
      │          │          │          │
   ffmpeg     Whisper    mimo API    JSON 文件
   ffprobe    (本地)    (多模态)    (持久化存储)
```

## 📦 快速开始

### 前置条件

- Python 3.10+
- ffmpeg (`brew install ffmpeg`)
- Whisper (`pip install openai-whisper`)

### 安装

```bash
git clone <repo-url>
cd vibe-video
pip install -r requirements.txt
```

### 配置

API Key 从 `~/.hermes/.env` 自动加载，或手动设置：

```bash
export XIAOMI_API_KEY=your_key
export XIAOMI_BASE_URL=your_base_url
```

### 启动

```bash
# 方式一
make run

# 方式二
python -m app.server --port 8766

# 方式三
python -m app.server --port 8766 --data-dir ~/Movies/GardenAutoCut
```

打开浏览器访问 http://127.0.0.1:8766

### 使用流程

1. **放入视频**：将 iPhone 视频放入 `~/Movies/GardenAutoCut/Inbox/`
2. **打开 Web UI**：浏览器访问 http://127.0.0.1:8766
3. **选择项目**：左侧选择日期项目
4. **设置参数**：输入主题、选择目标时长
5. **一键生成**：点击「🚀 一键全流程」
6. **镜头评分**：在「片段分析」标签页中审片、设为开头/封面
7. **编辑调整**：在「剧本」和「剪辑计划」标签页中编辑
8. **草稿预览**：点击「📝 草稿渲染」快速审片
9. **正式渲染**：点击「🎬 发布渲染」生成 1080p 成品
10. **发布包**：在「发布包」标签页获取标题/封面/话题/平台备注
11. **记录数据**：发布后回填播放/点赞/评论，系统持续学习

## 📋 10 阶段 Pipeline

| 阶段 | 名称 | 说明 | 输出文件 |
|------|------|------|---------|
| 1 | **视频导入** | 扫描 Inbox，按日期+主题归档 | `raw/` |
| 2 | **关键帧抽取** | 固定间隔 + 场景变化检测 | `keyframes/` |
| 3 | **Contact Sheet** | 拼图 + 时间戳叠加 + 帧索引 | `contact_sheets/` + `frame_index.json` |
| 4 | **片段级分析** | 每个视频切多个候选 shots，8 维平台评分 | `shots.json` |
| 5 | **音频转字幕** | Whisper 语音识别 | `transcription.json` |
| 5.5 | **模板选择** | 自动判断最适合的视频模板 | `video_template.json` |
| 6 | **剧情脚本** | 按推荐用途分组，AI 编排故事线 | `story_script.json` |
| 7 | **编辑计划** | 含剪辑意图 (why_selected / risk / alternatives) | `edit_plan.json` |
| 8 | **视频渲染** | 草稿 720p / 发布 1080p，concat 前 ffprobe 校验 | `outputs/` |
| 9 | **发布包** | 标题候选 / 封面字 / 话题 / 评论引导 / 平台备注 | `publish_pack.json` |
| 10 | **反馈闭环** | 记录发布数据，周报分析 | `performance.json` |

## 🎯 平台评分 (8 维)

| 字段 | 权重 | 说明 |
|------|------|------|
| hook | 0.18 | 是否适合前 3 秒 |
| retention | 0.18 | 能否维持停留 |
| action | 0.16 | 是否有动作变化 |
| story_value | 0.16 | 是否推动故事 |
| beauty | 0.12 | 花草画面是否美 |
| clarity | 0.10 | 观众能不能一眼看懂 |
| contrast | 0.07 | 是否有前后变化 |
| cover_value | 0.03 | 是否适合做封面 |

## 🎬 5 种视频模板

| 模板 | 适用场景 |
|------|---------|
| `before_after` | 整理前后对比 |
| `garden_diary` | 花园日记，多动作片段拼接 |
| `tutorial` | 种植/养护教程 |
| `healing_mood` | 治愈氛围片 |
| `one_problem` | 一个问题解决型 |

## 📁 项目结构

```
vibe-video/
├── README.md
├── requirements.txt
├── Makefile
├── app/
│   ├── server.py          # Flask 后端 (10 阶段 Pipeline + API)
│   ├── schemas.py         # Pydantic 数据模型
│   ├── json_utils.py      # JSON 校验 + 原子写入 + AI 输出解析
│   └── templates/
│       └── index.html     # Web UI 前端 (镜头评分面板)
├── tests/                 # 最小测试集
│   ├── test_safety.py     # 路径安全 + 并发锁
│   ├── test_schemas.py    # JSON 契约校验
│   └── fixtures/          # 无 AI 测试数据
├── scripts/
├── docs/
│   ├── architecture.md    # 技术架构详解
│   └── ...
└── data/
```

## 🔧 开发

```bash
# 安装依赖
make install

# 运行测试
make test

# 代码检查
make lint
```

## 📄 许可证

MIT License
