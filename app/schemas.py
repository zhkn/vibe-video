"""
Pydantic 数据模型 — AI 输出校验 + JSON 持久化结构
所有 AI 生成的 JSON 必须经过这些模型校验后再写入文件。
"""

from __future__ import annotations

try:
    from pydantic import BaseModel, Field, field_validator
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    # 最小兼容 shim
    class BaseModel:
        def dict(self):
            return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        def model_dump(self):
            return self.dict()
    def Field(default=None, **kw):
        return default
    def field_validator(*a, **kw):
        def decorator(fn):
            return fn
        return decorator


# ─── 画面质量 (4 维) ───

class QualityScores(BaseModel):
    clarity: int = Field(5, ge=1, le=10, description="清晰度")
    stability: int = Field(5, ge=1, le=10, description="稳定度")
    exposure: int = Field(5, ge=1, le=10, description="曝光")
    composition: int = Field(5, ge=1, le=10, description="构图")


# ─── 平台评分 (8 维) ───

class PlatformScores(BaseModel):
    hook: int = Field(5, ge=1, le=10, description="前3秒吸引力")
    retention: int = Field(5, ge=1, le=10, description="维持停留能力")
    action: int = Field(5, ge=1, le=10, description="动作变化丰富度")
    beauty: int = Field(5, ge=1, le=10, description="画面美感")
    clarity: int = Field(5, ge=1, le=10, description="一眼看懂程度")
    contrast: int = Field(5, ge=1, le=10, description="前后变化对比")
    story_value: int = Field(5, ge=1, le=10, description="故事推进价值")
    cover_value: int = Field(5, ge=1, le=10, description="封面适合度")


# ─── 片段级分析 (shots.json 中的每个 shot) ───

class ShotAnalysis(BaseModel):
    shot_id: str = Field("", description="片段 ID，如 5894_s001")
    source: str = Field("unknown", description="源视频文件名")
    start: float = Field(0.0, ge=0, description="起始秒数")
    end: float = Field(0.0, ge=0, description="结束秒数")
    duration: float = Field(0.0, ge=0, description="时长秒数")
    visual_summary: str = Field("", description="画面内容描述")
    shot_types: list[str] = Field(default_factory=list, description="片段类型标签")
    garden_objects: list[str] = Field(default_factory=list, description="画面中的物体")
    actions: list[str] = Field(default_factory=list, description="画面中的动作")
    quality: QualityScores = Field(default_factory=QualityScores, description="画面质量评分")
    platform_scores: PlatformScores = Field(default_factory=PlatformScores, description="平台化评分")
    recommended_use: list[str] = Field(default_factory=list, description="推荐用途")
    delete: bool = Field(False, description="是否建议删除")
    delete_reason: str = Field("", description="删除原因")

    @field_validator("source", mode="before")
    @classmethod
    def source_must_be_str(cls, v):
        return str(v) if v else "unknown"


class ShotsResponse(BaseModel):
    """shots.json 顶层结构"""
    project_id: str = Field("", description="项目 ID")
    shots: list[ShotAnalysis] = Field(default_factory=list, description="候选片段列表")


# ─── 视频模板 ───

class VideoTemplateDecision(BaseModel):
    """video_template.json 结构"""
    video_template: str = Field("one_problem", description="模板 key")
    template_name: str = Field("一个问题解决型", description="模板中文名")
    reason: str = Field("", description="选择原因")
    structure: list[str] = Field(default_factory=list, description="推荐的故事角色顺序")
    tone: str = Field("", description="整体调性")
    hook_strategy: str = Field("", description="开头策略")


# ─── 编辑计划 ───

class EditClip(BaseModel):
    """edit_plan.json 中的每个 clip"""
    shot_id: str = Field("", description="对应的 shot ID")
    role: str = Field("action", description="故事角色")
    source: str = Field("", description="源视频文件名")
    source_path: str = Field("", description="源文件完整路径")
    source_duration: float = Field(0.0, description="源视频总时长")
    source_has_audio: bool = Field(True, description="源视频是否有音频")
    start: float = Field(0.0, description="片段起始秒")
    end: float = Field(0.0, description="片段结束秒")
    duration: float = Field(0.0, description="片段时长")
    timeline_start: float = Field(0.0, description="时间线起始位置")
    timeline_end: float = Field(0.0, description="时间线结束位置")
    caption: str = Field("", description="字幕文字")
    note: str = Field("", description="备注")
    # 剪辑意图
    edit_style: str = Field("normal", description="剪辑风格: normal/fast_cut/slow_motion/fade")
    speed: float = Field(1.0, description="播放速度")
    why_selected: str = Field("", description="为什么选这个 shot")
    risk: str = Field("", description="潜在风险提示")
    platform_goal: str = Field("", description="平台目标")
    alternatives: list[str] = Field(default_factory=list, description="备选 shot ID 列表")


class EditPlan(BaseModel):
    """edit_plan.json 顶层结构"""
    version: str = Field("auto-plan-v3", description="计划版本")
    created_at: str = Field("", description="创建时间")
    title: str = Field("", description="视频标题")
    video_template: str = Field("", description="使用的视频模板")
    template_name: str = Field("", description="模板中文名")
    raw_dir: str = Field("", description="原始视频目录")
    target_duration: int = Field(66, description="目标时长(秒)")
    actual_duration: float = Field(0.0, description="实际时长(秒)")
    render: dict = Field(default_factory=dict, description="渲染参数")
    clips: list[EditClip] = Field(default_factory=list, description="剪辑片段列表")


# ─── 发布包 ───

class PublishPack(BaseModel):
    """publish_pack.json 结构"""
    project_id: str = Field("", description="项目 ID")
    generated_at: str = Field("", description="生成时间")
    title_candidates: list[str] = Field(default_factory=list, description="标题候选")
    cover_text_candidates: list[str] = Field(default_factory=list, description="封面文字候选")
    description: str = Field("", description="发布简介")
    hashtags: list[str] = Field(default_factory=list, description="话题标签")
    comment_prompt: str = Field("", description="评论引导问题")
    platform_notes: dict = Field(default_factory=dict, description="各平台发布备注")


# ─── 发布后反馈 ───

class PerformanceRecord(BaseModel):
    """performance.json 中的每条记录"""
    project_id: str = Field("", description="项目 ID")
    platform: str = Field("douyin", description="发布平台")
    published_at: str = Field("", description="发布日期")
    template: str = Field("", description="使用的模板")
    template_name: str = Field("", description="模板中文名")
    title: str = Field("", description="实际发布的标题")
    duration: int = Field(0, description="视频时长(秒)")
    views: int = Field(0, description="播放量")
    likes: int = Field(0, description="点赞数")
    comments: int = Field(0, description="评论数")
    saves: int = Field(0, description="收藏数")
    shares: int = Field(0, description="转发数")
    avg_watch_time: float = Field(0.0, description="平均观看时长(秒)")
    completion_rate: float = Field(0.0, description="完播率")
    recorded_at: str = Field("", description="记录时间")


# ─── 旧版兼容 ───

class AnalysisSegment(BaseModel):
    """analysis.json 中的每个片段 (向后兼容)"""
    file: str = Field("", description="源视频文件名")
    start: float = Field(0.0, description="起始秒数")
    end: float = Field(0.0, description="结束秒数")
    duration: float = Field(0.0, description="时长秒数")
    action: str = Field("", description="画面动作")
    subjects: list[str] = Field(default_factory=list, description="画面元素")
    story_role: str = Field("action", description="故事角色")
    quality_score: int = Field(6, description="画面质量")
    stability: int = Field(7, description="稳定度")
    privacy_risk: int = Field(1, description="隐私风险")
    caption: str = Field("", description="字幕文字")


# ─── 校验工具 ───

def validate_shots(raw_shots: list[dict]) -> list[dict]:
    """校验并修正 shots 列表，返回修正后的 dict 列表"""
    validated = []
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        try:
            shot = ShotAnalysis(**s)
            validated.append(shot.model_dump() if hasattr(shot, 'model_dump') else shot.dict())
        except Exception:
            # 尝试修正常见问题后重试
            try:
                ps = s.get("platform_scores", {})
                if isinstance(ps, dict) and "story" in ps and "story_value" not in ps:
                    ps["story_value"] = ps.pop("story")
                for k in ["hook", "retention", "action", "beauty", "clarity", "contrast", "story_value", "cover_value"]:
                    ps.setdefault(k, 5)
                shot = ShotAnalysis(**s)
                validated.append(shot.model_dump() if hasattr(shot, 'model_dump') else shot.dict())
            except Exception as e:
                print(f"  Shot 校验失败，跳过: {e}")
                continue
    return validated
