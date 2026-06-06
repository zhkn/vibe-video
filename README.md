# 🌿 Vibe Video — AI 驱动的自动视频剪辑系统

> iPhone 拍摄 → 自动归档 → AI 分析 → 一键生成可发布短视频

## ✨ 核心特性

- **一键全流程**：8 阶段 Pipeline，从原始视频到成品输出
- **AI 智能分析**：多模态视觉分析 + 语音转字幕 + 智能剧情生成
- **Web UI 可视化**：暗色主题 SPA，实时进度追踪，可视化编辑剧本和剪辑计划
- **故事驱动剪辑**：9 种故事角色自动编排（开场空镜 → 核心动作 → 收尾空镜）
- **人机协作**：支持手动编辑剧本、调整片段、重新渲染
- **灵活配置**：自定义目标时长（30s ~ 3min）、主题、AI 模型

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     浏览器 Web UI                            │
│          (暗色主题 SPA, SSE 实时进度, 可视化编辑)              │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP / SSE
┌──────────────────────────▼──────────────────────────────────┐
│                    Flask 后端 (server.py)                     │
├─────────────────────────────────────────────────────────────┤
│  Pipeline 8 阶段:                                            │
│  导入 → 抽帧 → 拼图 → 视觉分析 → 转写 → 剧本 → 编辑计划 → 渲染 │
├─────────────────────────────────────────────────────────────┤
│  外部工具: ffmpeg · Whisper · mimo-v2.5 · mimo-v2.5-pro      │
└─────────────────────────────────────────────────────────────┘
```

## 📦 快速开始

### 前置条件

- Python 3.10+
- ffmpeg (`brew install ffmpeg`)
- Whisper (`pip install openai-whisper`)

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd vibe-video

# 安装依赖
make install
# 或
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API key
```

### 启动

```bash
# 方式一: Makefile
make run

# 方式二: 启动脚本
./scripts/start.sh --port 8766

# 方式三: 直接运行
python -m app.server --port 8766 --data-dir ~/Movies/GardenAutoCut
```

打开浏览器访问 http://127.0.0.1:8766

### 使用流程

1. **放入视频**：将 iPhone 视频放入 `~/Movies/GardenAutoCut/Inbox/`
2. **打开 Web UI**：浏览器访问 http://127.0.0.1:8766
3. **选择项目**：左侧选择日期项目
4. **设置参数**：输入主题、选择目标时长
5. **一键生成**：点击「🚀 一键全流程」
6. **编辑调整**：在「剧本」和「剪辑计划」标签页中编辑
7. **重新渲染**：调整满意后点击「🎬 渲染输出」

## 📋 8 阶段 Pipeline

| 阶段 | 名称 | 说明 | 工具 |
|------|------|------|------|
| 1 | **视频导入** | 扫描 Inbox，按拍摄日期自动归档 | ffprobe |
| 2 | **关键帧抽取** | 固定间隔 + 场景变化检测 | ffmpeg |
| 3 | **Contact Sheet** | 关键帧拼成网格图 | Pillow |
| 4 | **视觉分析** | AI 识别画面内容、动作、角色 | mimo-v2.5 |
| 5 | **音频转字幕** | 提取音频 → Whisper 语音识别 | Whisper |
| 6 | **剧情脚本** | 融合视觉+字幕，生成故事脚本 | mimo-v2.5-pro |
| 7 | **编辑计划** | 从脚本生成可渲染的时间线 | Python |
| 8 | **视频渲染** | 裁剪拼接、字幕烧录、封面生成 | ffmpeg |

## 📁 项目结构

```
vibe-video/
├── README.md              # 项目说明
├── LICENSE                # MIT 许可证
├── pyproject.toml         # 项目配置
├── requirements.txt       # Python 依赖
├── Makefile               # 常用命令
├── .gitignore
├── .env.example           # 环境变量模板
├── app/                   # 主应用
│   ├── __init__.py
│   ├── server.py          # Flask 后端 (8 阶段 Pipeline + API)
│   └── templates/
│       └── index.html     # Web UI 前端
├── scripts/               # 工具脚本
│   ├── start.sh           # 启动脚本
│   └── garden_autoedit_mvp.py  # MVP 版本 (独立脚本)
├── docs/                  # 文档
│   ├── architecture.md    # 技术架构详解
│   ├── api.md             # API 接口文档
│   └── changelog.md       # 开发日志
└── data/                  # 数据目录 (gitignored)
    └── .gitkeep
```

## ⚙️ 配置说明

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | OpenAI API Key (Whisper) | - |
| `OPENAI_BASE_URL` | OpenAI API 地址 | `https://api.openai.com/v1` |
| `CUSTOM_API_KEY` | 自定义 LLM API Key | - |
| `CUSTOM_BASE_URL` | 自定义 LLM API 地址 | - |
| `VISUAL_MODEL` | 视觉分析模型 | `mimo-v2.5` |
| `TEXT_MODEL` | 文本生成模型 | `mimo-v2.5-pro` |
| `WHISPER_MODEL` | Whisper 模型大小 | `base` |

### 数据目录结构

```
~/Movies/GardenAutoCut/
├── Inbox/                 # 放入待处理视频
└── 2026-06-05/           # 按日期归档的项目
    ├── raw/               # 原始视频
    ├── keyframes/         # 抽取的关键帧
    ├── contact_sheets/    # 拼图
    ├── audio/             # 提取的音频
    ├── analysis.json      # 视觉分析结果
    ├── transcription.json # 转写结果
    ├── story_script.json  # 剧情脚本
    ├── edit_plan.json     # 剪辑计划
    └── outputs/           # 输出文件
        ├── rough_cut.mp4  # 最终视频
        ├── captions.srt   # 字幕
        └── cover.jpg      # 封面
```

## 🔧 开发

```bash
# 安装开发依赖
make install-dev

# 代码检查
make lint

# 代码格式化
make format

# 开发模式 (自动重载)
make dev
```

## 📄 许可证

MIT License
