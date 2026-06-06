# 📝 Garden AutoCut — 开发历史

## v2.0 — 2026-06-05

### 🎉 里程碑版本：端到端自动剪辑工作流

完整实现从 iPhone 视频导入到短视频成品输出的全自动流水线。

### 新增功能

**8 阶段 Pipeline**
- Stage 1: 视频导入 — Inbox → 按日期自动归档
- Stage 2: 关键帧抽取 — ffmpeg 固定间隔 + 场景变化双策略
- Stage 3: Contact Sheet — Pillow 拼图，优化多模态模型读取效率
- Stage 4: 视觉分析 — mimo-v2.5 多模态 AI，输出结构化 JSON
- Stage 5: 音频转字幕 — Whisper 本地模型 + API fallback
- Stage 6: 剧情脚本 — mimo-v2.5-pro 生成完整故事脚本
- Stage 7: 编辑计划 — 从脚本生成可渲染的 edit_plan.json
- Stage 8: 视频渲染 — ffmpeg 裁剪、拼接、字幕烧录、封面生成

**Web UI**
- 暗色主题 SPA (GitHub 风格)
- 项目列表 + 项目详情双视图
- 8 个标签页：素材视频、关键帧、Contact Sheet、视觉分析、字幕转写、剧情脚本、剪辑计划、输出
- 剧情脚本可视化编辑（标题、字幕、片段角色、时间点、旁白）
- 剪辑计划可视化编辑（时间线、源文件映射）
- 「一键全流程」按钮 + 单阶段独立运行
- SSE 实时进度追踪（进度条 + 百分比 + 状态消息）
- 视频片段预览（原生 `<video>` 时间片段）
- 关键帧 Lightbox 全屏查看
- 主题输入 + 目标时长选择
- 保存并重新渲染一键操作

**API 系统**
- RESTful JSON API，18 个端点
- SSE 实时进度流
- 脚本保存自动同步剪辑计划
- 安全的文件服务（路径校验）

**AI 集成**
- mimo-v2.5 (视觉分析) + mimo-v2.5-pro (文本生成) via Xiaomi API
- OpenAI API 作为 fallback
- 本地 Whisper base 模型优先，API fallback
- Fallback 分析：无 API 时基于文件信息生成基础分析
- Fallback 脚本：无 API 时按 STORY_SLOTS 模板生成

**故事系统**
- 9 种故事角色定义 (STORY_SLOTS)
- 自动角色分配 + 质量评分排序
- 隐私风险评估

**视频渲染**
- 9:16 竖屏输出 (1080×1920)
- H.264 编码，CRF 20，30fps
- 自动缩放裁切适配
- 字幕烧录 (PingFang SC 字体)
- 封面自动生成

### 端到端验证

- 输入: 7 个 iPhone 原始视频 (~650MB)
- 关键帧: 107 个
- Whisper 转写: 92 条中文片段
- AI 剧本: 「夏日花园的"断舍离"」，8 个片段，66.5 秒
- 输出: rough_cut.mp4 (44MB) + cover.jpg + captions.srt

---

## v1.0 — 2026-06-04 (概念验证)

### 初始设计

- 完成 Pipeline 8 阶段架构设计
- 定义 STORY_SLOTS 故事角色系统
- 确定输出规格 (1080×1920, 30fps, H.264)
- 创建 SKILL.md 技能文档

### 技术选型

- Flask 后端 + 原生前端 SPA
- ffmpeg 视频处理
- Whisper 语音识别
- 多模态 AI 视觉分析
- JSON 文件持久化

---

## 技术债务与已知限制

### 当前限制

- **字体依赖**: 字幕烧录需要 PingFang SC 字体（macOS 自带，Linux 需额外安装）
- **Whisper 精度**: base 模型中文转写可能有错字，可用 medium/large 模型提升
- **大文件性能**: >200MB 视频的 ffprobe 可能较慢（已缓存优化）
- **API 容错**: 视觉分析返回空结果时，脚本生成依赖转写内容
- **并发限制**: Flask threaded 模式，全流程在后台线程执行
- **无认证**: API 无身份认证，仅限本地使用

### 未来计划

- [ ] 支持多语言字幕
- [ ] 背景音乐自动混合
- [ ] 更多视频输出格式（横屏、方形）
- [ ] 批量项目处理
- [ ] AI 模型热切换 UI
- [ ] 视频预览时间线编辑器
- [ ] 自定义字幕样式
- [ ] 支持更多 AI 模型（GPT-4o, Claude 等）
