# Vibe Video — AI 驱动的自动视频剪辑系统

## 项目概述
AI 审片决策助手，专为花园/生活场景设计。核心产物不是视频文件，而是：
1. 今天这批素材最适合做哪种视频
2. 哪个镜头做开头
3. 哪些镜头必须删
4. 1-3 分钟时间线决策书
5. 标题、封面字、旁白、字幕
6. 发布后数据反馈

优先级: 素材理解 > 剪辑方案 > 发布包 > 粗剪预览 > 最终渲染

## 技术栈
- **后端**: Flask (单文件 app/server.py, ~2700 行)
- **前端**: 暗色主题 SPA (app/templates/index.html, ~1050 行)
- **AI 模型**: mimo-v2.5 (视觉分析), mimo-v2.5-pro (文本生成), Whisper (语音转字幕)
- **视频处理**: ffmpeg/ffprobe
- **图片处理**: Pillow (Contact Sheet 拼图)
- **数据校验**: Pydantic v2 (shots 结构校验)

## 关键目录
- `app/server.py` — Flask 后端，包含 10 阶段 Pipeline 和所有 API
- `app/templates/index.html` — Web UI 前端，SSE 实时进度
- `scripts/` — 启动脚本和 MVP 版本
- `docs/` — 架构文档、API 文档、开发日志
- `data/` — 数据目录 (gitignored，实际数据在 ~/Movies/GardenAutoCut/)

## 数据目录结构
```
~/Movies/GardenAutoCut/
├── Inbox/                    # 放入待处理视频
└── YYYY-MM-DD/               # 按日期归档
    └── <topic-slug>/         # 主题项目 (如 garden-trimming, pet-daily)
        ├── raw/              # 原始视频
        ├── keyframes/        # 抽取的关键帧
        ├── audio/            # 提取的音频
        ├── outputs/          # 输出文件 (rough_cut.mp4, draft_cut.mp4, cover.jpg, captions.srt)
        ├── meta.json         # 主题元数据
        ├── shots.json        # 片段级分析 (替代 analysis.json)
        ├── analysis.json     # 向后兼容的扁平化分析
        ├── frame_index.json  # 关键帧时间索引
        ├── video_template.json # 视频模板选择
        ├── story_script.json # 剧情脚本
        ├── edit_plan.json    # 编辑计划 (含剪辑意图)
        ├── publish_pack.json # 发布包 (标题/封面/话题/平台备注)
        └── performance.json  # 发布后数据反馈
```

**project_id 格式**: `YYYY-MM-DD/<topic-slug>` (如 `2026-06-05/garden-trimming`)
**旧格式兼容**: 也支持 `YYYY-MM-DD` 直接作为 project_id (目录下直接有 raw/)

## 10 阶段 Pipeline
1. 视频导入 (ffprobe 按日期归档)
2. 关键帧抽取 (ffmpeg 固定间隔 + 场景变化)
3. Contact Sheet + 时间索引 (Pillow 拼图 + frame_index.json)
4. 片段级视觉分析 (mimo-v2.5 多模态 → shots.json, Pydantic 校验)
4.5. Hook 候选评分 (独立 hook 评分 0-100, hook_candidates.json)
5. 音频转字幕 (Whisper)
5.5. 视频模板选择 (before_after/garden_diary/tutorial/healing_mood/one_problem)
6. **生成 3 个候选方案** (不同角度: 对比/日记/治愈, story_plans.json)
7. 编辑计划/剪辑决策书 (含 why_selected/viewer_question/alternatives)
8. 视频渲染 (草稿模式 720p / 发布模式 1080p)
8.5. 节奏检查器 (前3秒钩子/连续画面/记忆点/互动引导)
9. 发布包 (标题候选/封面文字/话题/评论引导/平台变体)
10. 发布后反馈 (performance.json + 周报分析)

## 平台化评分 (8 维)
- hook: 前3秒吸引力 (权重 0.18)
- retention: 维持停留能力 (0.18)
- action: 动作变化丰富度 (0.16)
- story_value: 故事推进价值 (0.16)
- beauty: 画面美感 (0.12)
- clarity: 一眼看懂程度 (0.10)
- contrast: 前后变化对比 (0.07)
- cover_value: 封面适合度 (0.03)

## 5 种视频模板
| 模板 | 适用场景 | 结构 |
|------|---------|------|
| before_after | 整理前后对比 | opening→space→action_intro→action→collect→result→detail→ending |
| garden_diary | 花园日记 | opening→space→action→life→action→detail→ending |
| tutorial | 种植/养护教程 | opening→action_intro→action→action→result→detail→ending |
| healing_mood | 治愈氛围片 | opening→space→detail→life→detail→ending |
| one_problem | 一个问题解决型 | opening→space→action_intro→action→collect→result→ending |

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
- 输出格式: 9:16 竖屏, 1080x1920 (发布) / 720x1280 (草稿)

## 代码规范
- Python 3.10+, 使用 type hints
- 中文注释和文档
- 单文件架构 (server.py), 不过度拆分
- 前端纯 HTML/CSS/JS, 无框架依赖
- Pydantic 校验 AI 输出结构
- 每次修改完成后自动 git commit
