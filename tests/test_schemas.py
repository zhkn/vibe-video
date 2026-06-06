"""
JSON 契约测试 — 保证数据结构不会坏
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.schemas import (
    ShotAnalysis, ShotsResponse, PlatformScores, QualityScores,
    EditPlan, EditClip, PublishPack, VideoTemplateDecision,
    PerformanceRecord, validate_shots,
)
from app.json_utils import extract_json_from_text, load_json, atomic_write_json


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "project_minimal")


# ─── Shot 结构校验 ───

class TestShotSchema:
    """test_score_shots_schema_validation"""

    def test_valid_shot(self):
        """合法 shot 应该通过校验"""
        shot = ShotAnalysis(
            source="test.MP4",
            start=0.0,
            end=5.0,
            duration=5.0,
            visual_summary="测试画面",
            platform_scores=PlatformScores(hook=8, retention=7, action=6, beauty=5,
                                           clarity=7, contrast=4, story_value=6, cover_value=3),
        )
        assert shot.source == "test.MP4"
        assert shot.platform_scores.hook == 8

    def test_default_values(self):
        """缺失字段应该有默认值"""
        shot = ShotAnalysis(source="test.MP4")
        assert shot.start == 0.0
        assert shot.delete is False
        assert shot.platform_scores.hook == 5
        assert shot.quality.clarity == 5

    def test_invalid_score_clamped(self):
        """超出范围的分数应该被 Pydantic 拒绝"""
        with pytest.raises(Exception):
            PlatformScores(hook=11)  # > 10

    def test_source_none_converted(self):
        """source=None 应该转为 'unknown'"""
        shot = ShotAnalysis(source=None)
        assert shot.source == "unknown"

    def test_validate_shots_batch(self):
        """validate_shots 应该批量校验"""
        raw = [
            {"source": "a.MP4", "start": 0, "end": 5},
            {"source": "b.MP4", "platform_scores": {"story": 7}},  # 旧字段名
            "invalid",  # 非 dict，应该跳过
        ]
        result = validate_shots(raw)
        assert len(result) == 2
        # 第二个应该有 story_value
        assert "story_value" in result[1].get("platform_scores", {}) or \
               result[1].get("platform_scores", {}).get("story_value") == 7

    def test_validate_shots_fills_missing_platform_fields(self):
        """缺失的 platform_scores 字段应该被填充"""
        raw = [{"source": "test.MP4", "platform_scores": {"hook": 8}}]
        result = validate_shots(raw)
        assert len(result) == 1
        ps = result[0]["platform_scores"]
        assert "retention" in ps
        assert "action" in ps


# ─── EditPlan 校验 ───

class TestEditPlanSchema:
    """test_edit_plan_timeline_recalculation"""

    def test_edit_clip_has_shot_id(self):
        """EditClip 应该有 shot_id 字段"""
        clip = EditClip(shot_id="test_s001", source="test.MP4")
        d = clip.model_dump() if hasattr(clip, 'model_dump') else clip.dict()
        assert d["shot_id"] == "test_s001"

    def test_edit_clip_has_alternatives(self):
        """EditClip 应该有 alternatives 字段"""
        clip = EditClip(source="test.MP4", alternatives=["alt_s001", "alt_s002"])
        d = clip.model_dump() if hasattr(clip, 'model_dump') else clip.dict()
        assert d["alternatives"] == ["alt_s001", "alt_s002"]

    def test_edit_plan_timeline_consistent(self):
        """edit_plan fixture 的时间线应该一致"""
        plan_path = os.path.join(FIXTURES_DIR, "edit_plan.json")
        if not os.path.exists(plan_path):
            pytest.skip("fixture not found")

        data = load_json(plan_path)
        plan = EditPlan(**data)

        clips = plan.clips
        assert len(clips) == 2
        # 第一个 clip
        assert clips[0].timeline_start == 0.0
        assert clips[0].timeline_end == 5.0
        # 第二个 clip 紧接第一个
        assert clips[1].timeline_start == 5.0
        assert clips[1].timeline_end == 13.0
        # actual_duration 应该等于最后一个 clip 的 timeline_end
        assert plan.actual_duration == 13.0

    def test_edit_plan_version(self):
        """edit_plan 应该有版本号"""
        plan_path = os.path.join(FIXTURES_DIR, "edit_plan.json")
        if not os.path.exists(plan_path):
            pytest.skip("fixture not found")
        data = load_json(plan_path)
        assert "version" in data
        assert data["version"] == "auto-plan-v3"


# ─── Shots fixture 校验 ───

class TestShotsFixture:

    def test_shots_fixture_valid(self):
        """shots.json fixture 应该通过校验"""
        shots_path = os.path.join(FIXTURES_DIR, "shots.json")
        if not os.path.exists(shots_path):
            pytest.skip("fixture not found")

        data = load_json(shots_path)
        response = ShotsResponse(**data)
        assert len(response.shots) == 2
        assert response.shots[0].shot_id == "test_video_s001"

    def test_shots_platform_scores_complete(self):
        """每个 shot 的 platform_scores 应该有完整 8 维"""
        shots_path = os.path.join(FIXTURES_DIR, "shots.json")
        if not os.path.exists(shots_path):
            pytest.skip("fixture not found")

        data = load_json(shots_path)
        for shot_data in data["shots"]:
            ps = shot_data["platform_scores"]
            for field in ["hook", "retention", "action", "beauty", "clarity", "contrast", "story_value", "cover_value"]:
                assert field in ps, f"Missing {field} in {shot_data['shot_id']}"
                assert 1 <= ps[field] <= 10, f"{field}={ps[field]} out of range in {shot_data['shot_id']}"


# ─── JSON 工具测试 ───

class TestJsonUtils:

    def test_extract_json_object(self):
        """应该提取 JSON 对象"""
        text = 'some text {"key": "value"} more text'
        result = extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_extract_json_array(self):
        """应该提取 JSON 数组"""
        text = 'here is [1, 2, 3] array'
        result = extract_json_from_text(text)
        assert result == [1, 2, 3]

    def test_extract_json_nested(self):
        """应该提取嵌套 JSON"""
        text = '```json\n{"shots": [{"id": 1}]}\n```'
        result = extract_json_from_text(text)
        assert result == {"shots": [{"id": 1}]}

    def test_extract_json_no_json(self):
        """没有 JSON 时返回 None"""
        result = extract_json_from_text("no json here")
        assert result is None

    def test_atomic_write_json(self, tmp_path):
        """atomic_write_json 应该写入文件"""
        path = tmp_path / "test.json"
        atomic_write_json(path, {"key": "value"})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"key": "value"}

    def test_atomic_write_creates_dirs(self, tmp_path):
        """atomic_write_json 应该自动创建目录"""
        path = tmp_path / "sub" / "dir" / "test.json"
        atomic_write_json(path, {"nested": True})
        assert path.exists()

    def test_load_json_missing_file(self):
        """load_json 对不存在的文件返回 default"""
        result = load_json("/nonexistent/path.json", default={"fallback": True})
        assert result == {"fallback": True}

    def test_load_json_invalid_json(self, tmp_path):
        """load_json 对无效 JSON 返回 default"""
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        result = load_json(path, default=None)
        assert result is None
