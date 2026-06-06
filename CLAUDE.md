# Vibe Video — AI 驱动的自动视频剪辑系统

## 项目概述
端到端自动化视频剪辑工作流，专为花园/生活场景设计。将 iPhone 视频自动分析、生成剧本、渲染为 TikTok/抖音竖屏短视频。

## 技术栈
- **后端**: Flask (单文件 app/server.py, ~1800 行)
- **前端**: 暗色主题 SPA (app/templates/index.html, ~820 行)
- **AI 模型**: mimo-v2.5 (视觉分析), mimo-v2.5-pro (文本生成), Whisper (语音转字幕)
- **视频处理**: ffmpeg/ffprobe
- **图片处理**: Pillow (Contact Sheet 拼图)

## 关键目录
- `app/server.py` — Flask 后端，包含 8 阶段 Pipeline 和所有 API
- `app/templates/index.html` — Web UI 前端，SSE 实时进度
- `scripts/` — 启动脚本和 MVP 版本
- `docs/` — 架构文档、API 文档、开发日志
- `data/` — 数据目录 (gitignored，实际数据在 ~/Movies/GardenAutoCut/)

## 数据目录结构
```
~/Movies/GardenAutoCut/
├── Inbox/          # 放入待处理视频
└── YYYY-MM-DD/     # 按日期归档
    ├── raw/        # 原始视频
    ├── keyframes/  # 抽取的关键帧
    ├── audio/      # 提取的音频
    ├── outputs/    # 输出文件 (rough_cut.mp4, cover.jpg, captions.srt)
    ├── analysis.json
    ├── story_script.json
    └── edit_plan.json
```

## 8 阶段 Pipeline
1. 视频导入 (ffprobe 按日期归档)
2. 关键帧抽取 (ffmpeg 固定间隔 + 场景变化)
3. Contact Sheet (Pillow 拼图)
4. 视觉分析 (mimo-v2.5 多模态)
5. 音频转字幕 (Whisper)
6. 剧情脚本 (mimo-v2.5-pro)
7. 编辑计划 (Python 从脚本生成)
8. 视频渲染 (ffmpeg 裁剪拼接 + 字幕烧录)

## 常用命令
```bash
# 启动服务
make run
# 或
python -m app.server --port 8766

# 安装依赖
make install

# 代码检查
make lint
```

## 配置
- API Key 从 `~/.hermes/.env` 自动加载
- Whisper 默认使用本地 base 模型
- 视觉分析: mimo-v2.5, 文本生成: mimo-v2.5-pro
- 默认端口: 8766
- 输出格式: 9:16 竖屏, 720x1280

## 代码规范
- Python 3.10+, 使用 type hints
- 中文注释和文档
- 单文件架构 (server.py), 不过度拆分
- 前端纯 HTML/CSS/JS, 无框架依赖
