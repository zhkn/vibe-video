#!/usr/bin/env python3
"""
Garden AutoCut — 完整自动视频剪辑工作流 + Web UI
用法:
  python3 app.py                    # 启动 Web UI (默认端口 8766)
  python3 app.py --port 8888        # 自定义端口

流程:
  1. 导入视频 → 按日期归档
  2. 抽关键帧 → 固定间隔 + 场景变化
  3. 视觉分析 → AI 识别画面内容
  4. 音频转字幕 → Whisper 语音识别
  5. 综合分析 → 视觉+字幕融合
  6. 剧情脚本 → AI 生成故事线
  7. 视频渲染 → ffmpeg 自动剪辑
"""

from __future__ import annotations
import argparse
import base64
import datetime
import glob
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory, Response, render_template

from app.schemas import (
    ShotAnalysis, PlatformScores, QualityScores, ShotsResponse,
    VideoTemplateDecision, EditPlan, EditClip, PublishPack,
    PerformanceRecord, AnalysisSegment, validate_shots, HAS_PYDANTIC,
)
from app.json_utils import load_json, atomic_write_json, extract_json_from_text

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

ROOT_DIR = os.path.expanduser("~/Movies/GardenAutoCut")
INBOX_DIR = os.path.join(ROOT_DIR, "Inbox")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保 ffmpeg 在 PATH 中
for _bin_dir in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if _bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bin_dir + os.pathsep + os.environ.get("PATH", "")

# 加载 .env 中的 API key
_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".hevc", ".3gp"}

STORY_SLOTS = [
    {"role": "opening", "label": "开场空镜", "desc": "吸引注意力的美丽画面"},
    {"role": "space", "label": "环境交代", "desc": "展示整体空间"},
    {"role": "action_intro", "label": "动作引入", "desc": "即将开始的劳动"},
    {"role": "action", "label": "核心动作", "desc": "主要劳动过程"},
    {"role": "collect", "label": "收集整理", "desc": "成果收集"},
    {"role": "life", "label": "生活气息", "desc": "背影、细节、自然感"},
    {"role": "result", "label": "成果展示", "desc": "劳动成果"},
    {"role": "detail", "label": "细节特写", "desc": "精致细节"},
    {"role": "ending", "label": "收尾空镜", "desc": "留有余韵的结束"},
]

VIDEO_TEMPLATES = {
    "before_after": {
        "name": "整理前后对比",
        "desc": "素材中包含整理前、修剪动作、红桶收集、整理后全景",
        "structure": ["opening", "space", "action_intro", "action", "collect", "result", "detail", "ending"],
        "tone": "成就感、清爽",
        "hook_strategy": "先放整理后成果，再回溯过程",
    },
    "garden_diary": {
        "name": "花园日记",
        "desc": "日常花园记录，多动作片段拼接",
        "structure": ["opening", "space", "action", "life", "action", "detail", "ending"],
        "tone": "轻松日常",
        "hook_strategy": "最美的花或最有趣的动作",
    },
    "tutorial": {
        "name": "种植/养护教程",
        "desc": "有明确的教学内容，如栽种、修剪、施肥",
        "structure": ["opening", "action_intro", "action", "action", "result", "detail", "ending"],
        "tone": "实用、亲切",
        "hook_strategy": "展示最终效果或关键步骤",
    },
    "healing_mood": {
        "name": "治愈氛围片",
        "desc": "画面极美、动作少，重在氛围营造",
        "structure": ["opening", "space", "detail", "life", "detail", "ending"],
        "tone": "安静、治愈",
        "hook_strategy": "最美的空镜或特写",
    },
    "one_problem": {
        "name": "一个问题解决型",
        "desc": "发现问题 → 解决问题 → 结果",
        "structure": ["opening", "space", "action_intro", "action", "collect", "result", "ending"],
        "tone": "有悬念、有满足感",
        "hook_strategy": "先展示问题或对比",
    },
}

OUTPUT_SPEC = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "codec": "libx264",
    "crf": 20,
    "preset": "veryfast",
    "min_duration": 55,
    "max_duration": 75,
    "audio_sample_rate": 44100,
}

# 草稿模式：720p、快速 preset、带调试信息
DRAFT_SPEC = {
    "width": 720,
    "height": 1280,
    "fps": 24,
    "codec": "libx264",
    "crf": 26,
    "preset": "ultrafast",
    "min_duration": 55,
    "max_duration": 75,
    "audio_sample_rate": 22050,
}

# 全局进度追踪
progress_store: dict[str, dict] = {}
progress_lock = threading.Lock()


def update_progress(project_id: str, stage: str, percent: int, message: str, detail: str = ""):
    with progress_lock:
        progress_store[project_id] = {
            "stage": stage,
            "percent": percent,
            "message": message,
            "detail": detail,
            "timestamp": time.time(),
        }
        # 落盘 job 状态
        try:
            date_dir = os.path.join(ROOT_DIR, project_id)
            if os.path.isdir(date_dir):
                job_path = os.path.join(date_dir, "job_status.json")
                atomic_write_json(job_path, progress_store[project_id])
        except Exception:
            pass


# ─── 项目级并发锁 ───
project_locks: dict[str, threading.Lock] = {}
project_locks_lock = threading.Lock()


def acquire_project_lock(project_id: str) -> bool:
    """尝试获取项目级锁，返回是否成功。失败说明项目正在处理中。"""
    with project_locks_lock:
        lock = project_locks.setdefault(project_id, threading.Lock())
    return lock.acquire(blocking=False)


def release_project_lock(project_id: str):
    """释放项目级锁"""
    with project_locks_lock:
        lock = project_locks.get(project_id)
    if lock:
        try:
            lock.release()
        except RuntimeError:
            pass


# ─── 路径安全 ───

def resolve_project_dir(project_id: str) -> str:
    """解析 project_id 到安全的项目目录路径，防止路径穿越。
    成功返回绝对路径，失败抛出 ValueError。"""
    root = os.path.realpath(ROOT_DIR)
    # 规范化 project_id，防止 ../ 穿越
    safe_id = os.path.normpath(project_id)
    if safe_id.startswith("..") or safe_id.startswith("/"):
        raise ValueError(f"非法 project_id: {project_id}")
    project_dir = os.path.realpath(os.path.join(root, safe_id))
    if not project_dir.startswith(root + os.sep) and project_dir != root:
        raise ValueError(f"路径越界: {project_id}")
    return project_dir


def resolve_file_path(project_id: str, sub_dir: str, filename: str) -> str:
    """解析文件路径，确保在项目目录的安全子目录下。
    成功返回绝对路径，失败抛出 ValueError。"""
    project_dir = resolve_project_dir(project_id)
    # filename 不能包含路径分隔符
    if os.sep in filename or "/" in filename or ".." in filename:
        raise ValueError(f"非法文件名: {filename}")
    file_path = os.path.realpath(os.path.join(project_dir, sub_dir, filename))
    allowed_dir = os.path.realpath(os.path.join(project_dir, sub_dir))
    if not file_path.startswith(allowed_dir + os.sep):
        raise ValueError(f"文件路径越界: {project_id}/{sub_dir}/{filename}")
    return file_path


# ─── 统一错误响应 ───

def error_response(code: str, message: str, stage: str = "", retryable: bool = False, status: int = 500):
    """统一错误响应格式"""
    return jsonify({
        "error": {
            "code": code,
            "message": message,
            "stage": stage,
            "retryable": retryable,
        }
    }), status


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def run_cmd(cmd: list, check=True, capture=True, timeout=600) -> subprocess.CompletedProcess:
    """封装 subprocess.run"""
    printable = " ".join(str(x) for x in cmd[:8])
    if len(cmd) > 8:
        printable += " ..."
    print(f"  $ {printable}")
    return subprocess.run([str(x) for x in cmd], check=check, capture_output=capture, text=True, timeout=timeout)


_video_info_cache: dict[str, dict] = {}

def get_video_info(path: str) -> dict:
    """获取视频基本信息（带缓存）"""
    if path in _video_info_cache:
        return _video_info_cache[path]
    try:
        r = run_cmd([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-analyzeduration", "2000000", "-probesize", "2000000",
            "-show_format", "-show_streams", path
        ], check=False, timeout=10)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            fmt = data.get("format", {})
            video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
            audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
            fps_str = str(video_stream.get("r_frame_rate", "0/1"))
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 0
            else:
                fps = float(fps_str)
            result = {
                "duration": float(fmt.get("duration", 0)),
                "size_mb": round(int(fmt.get("size", 0)) / 1024 / 1024, 1),
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "fps": round(fps, 2),
                "has_audio": bool(audio_stream),
                "creation_time": fmt.get("tags", {}).get("creation_time", ""),
            }
            _video_info_cache[path] = result
            return result
    except Exception as e:
        print(f"  ffprobe error for {os.path.basename(path)}: {e}")
    result = {"duration": 0, "size_mb": round(os.path.getsize(path) / 1024 / 1024, 1) if os.path.exists(path) else 0, "width": 0, "height": 0, "fps": 0, "has_audio": False, "creation_time": ""}
    _video_info_cache[path] = result
    return result


def get_creation_time(path: str) -> datetime.datetime:
    info = get_video_info(path)
    ct = info.get("creation_time", "")
    if ct:
        try:
            return datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except:
            pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(path))


def ensure_dir(*parts) -> str:
    p = os.path.join(*parts)
    os.makedirs(p, exist_ok=True)
    return p


def image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def seconds_to_srt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms_total = int(round(seconds * 1000))
    h = ms_total // 3_600_000
    ms_total %= 3_600_000
    m = ms_total // 60_000
    ms_total %= 60_000
    s = ms_total // 1000
    ms = ms_total % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def slugify(text: str) -> str:
    """将中文/英文文本转为 URL-safe slug"""
    try:
        from pypinyin import pinyin, Style
        # 中文转拼音
        py = pinyin(text, style=Style.NORMAL)
        text = "-".join([p[0] for p in py if p[0]])
    except ImportError:
        # pypinyin 不可用时，保留原样
        pass

    # 转小写，替换非字母数字为连字符
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower().strip())
    slug = slug.strip('-')
    return slug or "untitled"


def _build_project_info(project_id: str, project_dir: str) -> dict:
    """构建项目信息字典"""
    raw_dir = os.path.join(project_dir, "raw")
    videos = [f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS] if os.path.isdir(raw_dir) else []
    outputs_dir = os.path.join(project_dir, "outputs")
    has_output = os.path.exists(os.path.join(outputs_dir, "rough_cut.mp4")) if os.path.isdir(outputs_dir) else False

    # 解析日期和主题
    parts = project_id.split("/", 1)
    date_str = parts[0]
    topic = parts[1] if len(parts) > 1 else ""

    return {
        "id": project_id,
        "date": date_str,
        "topic": topic,
        "video_count": len(videos),
        "has_output": has_output,
        "has_analysis": os.path.exists(os.path.join(project_dir, "shots.json")) or os.path.exists(os.path.join(project_dir, "analysis.json")),
        "has_edit_plan": os.path.exists(os.path.join(project_dir, "edit_plan.json")),
        "has_story_script": os.path.exists(os.path.join(project_dir, "story_script.json")),
    }


def list_projects() -> list[dict]:
    """列出所有项目（支持日期目录 + 主题子目录两层结构）"""
    projects = []
    if not os.path.isdir(ROOT_DIR):
        return projects

    for name in sorted(os.listdir(ROOT_DIR), reverse=True):
        date_dir = os.path.join(ROOT_DIR, name)
        if not os.path.isdir(date_dir) or name == "Inbox":
            continue

        # 旧格式兼容：直接 date/raw/
        if os.path.isdir(os.path.join(date_dir, "raw")):
            projects.append(_build_project_info(name, date_dir))
            continue

        # 新格式：遍历日期下的主题目录
        for topic_name in sorted(os.listdir(date_dir)):
            topic_dir = os.path.join(date_dir, topic_name)
            if os.path.isdir(topic_dir) and os.path.isdir(os.path.join(topic_dir, "raw")):
                project_id = f"{name}/{topic_name}"
                projects.append(_build_project_info(project_id, topic_dir))

    return projects


def get_project_data(project_id: str) -> dict:
    """获取项目完整数据"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    if not os.path.isdir(date_dir):
        return {"error": f"Project {project_id} not found"}

    raw_dir = os.path.join(date_dir, "raw")
    videos = []
    if os.path.isdir(raw_dir):
        for f in sorted(os.listdir(raw_dir)):
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTS:
                fpath = os.path.join(raw_dir, f)
                info = get_video_info(fpath)
                videos.append({
                    "filename": f,
                    "path": fpath,
                    **info,
                })

    # Load intermediate results
    def load_json(name):
        p = os.path.join(date_dir, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None

    # Keyframes
    kf_dir = os.path.join(date_dir, "keyframes")
    keyframes = {"fixed": [], "scene": []}
    if os.path.isdir(kf_dir):
        for sub in ["fixed", "scene"]:
            sub_dir = os.path.join(kf_dir, sub)
            if os.path.isdir(sub_dir):
                keyframes[sub] = sorted([
                    f for f in os.listdir(sub_dir) if f.endswith((".jpg", ".png"))
                ])

    # Outputs
    outputs_dir = os.path.join(date_dir, "outputs")
    outputs = {}
    if os.path.isdir(outputs_dir):
        for f in os.listdir(outputs_dir):
            if not f.startswith("_"):
                fpath = os.path.join(outputs_dir, f)
                outputs[f] = {
                    "size_mb": round(os.path.getsize(fpath) / 1024 / 1024, 1),
                    "path": fpath,
                }

    # 解析日期和主题
    parts = project_id.split("/", 1)
    date_str = parts[0]
    topic = parts[1] if len(parts) > 1 else ""

    # Shots (优先) 或 Analysis (兼容)
    shots_data = load_json("shots.json")
    analysis_data = load_json("analysis.json")

    # 检查项目是否正在处理中
    is_running = False
    with project_locks_lock:
        lock = project_locks.get(project_id)
        if lock and lock.locked():
            is_running = True

    return {
        "id": project_id,
        "date": date_str,
        "topic": topic,
        "dir": date_dir,
        "videos": videos,
        "keyframes": keyframes,
        "shots": shots_data,
        "analysis": analysis_data,
        "video_template": load_json("video_template.json"),
        "transcription": load_json("transcription.json"),
        "story_script": load_json("story_script.json"),
        "edit_plan": load_json("edit_plan.json"),
        "publish_pack": load_json("publish_pack.json"),
        "job_status": load_json("job_status.json"),
        "is_running": is_running,
        "outputs": outputs,
    }


# ═══════════════════════════════════════════════════════════
# Stage 1: 视频导入
# ═══════════════════════════════════════════════════════════

def stage_import(project_id: str = None, topic: str = "default") -> dict:
    """扫描 Inbox，按日期归档到主题目录"""
    update_progress(project_id or "import", "import", 0, "扫描 Inbox...")

    imported = []
    inbox = INBOX_DIR
    if not os.path.isdir(inbox):
        os.makedirs(inbox, exist_ok=True)
        return {"imported": [], "message": "Inbox 为空，已创建目录"}

    for fname in sorted(os.listdir(inbox)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTS:
            continue
        src = os.path.join(inbox, fname)
        if not os.path.isfile(src):
            continue

        ct = get_creation_time(src)
        date_str = ct.strftime("%Y-%m-%d")
        raw_dir = ensure_dir(ROOT_DIR, date_str, topic, "raw")
        dst = os.path.join(raw_dir, fname)

        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.move(src, dst)
            imported.append({"file": fname, "date": date_str, "topic": topic})
            print(f"  归档: {fname} → {date_str}/{topic}/raw/")

    update_progress(project_id or "import", "import", 100, f"导入完成: {len(imported)} 个视频")
    return {"imported": imported}


def stage_import_direct(files: list[str], project_id: str = None, topic: str = "default") -> dict:
    """直接导入指定文件到项目"""
    imported = []
    for fpath in files:
        if not os.path.exists(fpath):
            continue
        fname = os.path.basename(fpath)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTS:
            continue

        ct = get_creation_time(fpath)
        date_str = ct.strftime("%Y-%m-%d")
        # 如果 project_id 包含日期信息，使用它
        if project_id:
            parts = project_id.split("/", 1)
            date_str = parts[0]
            if len(parts) > 1:
                topic = parts[1]
        raw_dir = ensure_dir(ROOT_DIR, date_str, topic, "raw")
        dst = os.path.join(raw_dir, fname)

        if os.path.abspath(fpath) != os.path.abspath(dst):
            shutil.copy2(fpath, dst)
        imported.append({"file": fname, "date": date_str, "topic": topic})

    return {"imported": imported}


# ═══════════════════════════════════════════════════════════
# Stage 2: 抽关键帧
# ═══════════════════════════════════════════════════════════

def stage_keyframes(project_id: str, fps_interval: float = 2.0, scene_thresh: float = 0.35) -> dict:
    """固定抽帧 + 场景变化抽帧"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    raw_dir = os.path.join(date_dir, "raw")
    if not os.path.isdir(raw_dir):
        return {"error": "raw 目录不存在"}

    videos = [f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    kf_dir = ensure_dir(date_dir, "keyframes")
    fixed_dir = ensure_dir(kf_dir, "fixed")
    scene_dir = ensure_dir(kf_dir, "scene")

    results = []
    for i, fname in enumerate(sorted(videos)):
        vpath = os.path.join(raw_dir, fname)
        base = os.path.splitext(fname)[0]
        pct = int((i / len(videos)) * 100)
        update_progress(project_id, "keyframes", pct, f"抽帧: {fname}")

        # 固定抽帧
        run_cmd([
            "ffmpeg", "-y", "-i", vpath,
            "-vf", f"fps=1/{fps_interval}",
            os.path.join(fixed_dir, f"{base}_%04d.jpg")
        ], check=False, timeout=300)

        # 场景变化抽帧
        run_cmd([
            "ffmpeg", "-y", "-i", vpath,
            "-vf", f"select='gt(scene,{scene_thresh})'",
            "-vsync", "vfr",
            os.path.join(scene_dir, f"{base}_%04d.jpg")
        ], check=False, timeout=300)

        fixed_count = len(glob.glob(os.path.join(fixed_dir, f"{base}_*.jpg")))
        scene_count = len(glob.glob(os.path.join(scene_dir, f"{base}_*.jpg")))
        results.append({"file": fname, "fixed_frames": fixed_count, "scene_frames": scene_count})

    update_progress(project_id, "keyframes", 100, f"关键帧抽取完成: {len(videos)} 个视频")
    return {"results": results}


# ═══════════════════════════════════════════════════════════
# Stage 3: 生成 Contact Sheet
# ═══════════════════════════════════════════════════════════

def stage_contact_sheets(project_id: str, max_frames: int = 30, fps_interval: float = 2.0) -> dict:
    """把关键帧拼成 contact sheet，带时间索引叠加"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {"error": "需要安装 Pillow: pip install pillow"}

    date_dir = os.path.join(ROOT_DIR, project_id)
    kf_dir = os.path.join(date_dir, "keyframes")
    if not os.path.isdir(kf_dir):
        return {"error": "keyframes 目录不存在"}

    cs_dir = ensure_dir(date_dir, "contact_sheets")
    sheets = []
    frame_index = []  # frame_index.json

    # 尝试加载字体
    try:
        font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 16)
        font_small = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 12)
    except:
        font = ImageFont.load_default()
        font_small = font

    for sub in ["fixed", "scene"]:
        sub_dir = os.path.join(kf_dir, sub)
        if not os.path.isdir(sub_dir):
            continue
        frames = sorted(glob.glob(os.path.join(sub_dir, "*.jpg")))
        if not frames:
            continue

        # 取均匀采样的帧
        if len(frames) > max_frames:
            step = len(frames) / max_frames
            sampled_indices = [int(i * step) for i in range(max_frames)]
            frames = [frames[idx] for idx in sampled_indices]
        else:
            sampled_indices = list(range(len(frames)))
            frames = frames[:max_frames]

        n = len(frames)
        cols = min(5, n)
        rows = math.ceil(n / cols)
        thumb_w, thumb_h = 324, 576

        sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)

        for i, fp in enumerate(frames):
            try:
                img = Image.open(fp)
                img.thumbnail((thumb_w, thumb_h))
                x = (i % cols) * thumb_w
                y = (i // cols) * thumb_h
                ox = x + (thumb_w - img.width) // 2
                oy = y + (thumb_h - img.height) // 2
                sheet.paste(img, (ox, oy))

                # 解析帧文件名: {base}_{0001}.jpg
                fname = os.path.basename(fp)
                base_parts = fname.rsplit("_", 1)
                source_base = base_parts[0] if len(base_parts) > 1 else "unknown"
                frame_num_str = base_parts[1].split(".")[0] if len(base_parts) > 1 else "0"
                try:
                    frame_num = int(frame_num_str)
                except ValueError:
                    frame_num = i + 1

                # 计算时间戳
                if sub == "fixed":
                    timestamp = (frame_num - 1) * fps_interval
                else:
                    # scene 帧没有固定时间间隔，用帧号估算
                    timestamp = frame_num * fps_interval

                # 显示：视频编号 + 时间戳 + 帧编号
                ts_str = f"{int(timestamp//60):02d}:{timestamp%60:04.1f}"
                label = f"{source_base} {ts_str} #{frame_num}"

                # 标签背景
                label_bbox = draw.textbbox((x + 5, y + 5), label, font=font_small)
                draw.rectangle([label_bbox[0]-2, label_bbox[1]-2, label_bbox[2]+2, label_bbox[3]+2],
                              fill=(0, 0, 0, 180))
                draw.text((x + 5, y + 5), label, fill=(255, 200, 0), font=font_small)

                # 帧编号大字（右下角）
                num_text = f"#{i+1}"
                num_bbox = draw.textbbox((0, 0), num_text, font=font)
                nw = num_bbox[2] - num_bbox[0]
                nh = num_bbox[3] - num_bbox[1]
                draw.rectangle([x + thumb_w - nw - 12, y + thumb_h - nh - 10,
                               x + thumb_w - 4, y + thumb_h - 4], fill=(0, 0, 0, 180))
                draw.text((x + thumb_w - nw - 8, y + thumb_h - nh - 6), num_text,
                         fill=(255, 255, 255), font=font)

                # 记录 frame_index
                frame_index.append({
                    "frame_id": f"{source_base}_{frame_num_str}",
                    "source": f"{source_base}.MP4" if not source_base.endswith((".MP4", ".mp4", ".MOV", ".mov")) else source_base,
                    "timestamp": round(timestamp, 1),
                    "sheet": f"{sub}_sheet.jpg",
                    "grid_position": [i // cols, i % cols],
                    "grid_index": i,
                })

            except Exception as e:
                print(f"  无法打开帧 {fp}: {e}")

        sheet_path = os.path.join(cs_dir, f"{sub}_sheet.jpg")
        sheet.save(sheet_path, quality=85)
        sheets.append({"type": sub, "path": sheet_path, "frame_count": n})
        print(f"  Contact sheet: {sub}_sheet.jpg ({n} 帧)")

    # 保存 frame_index.json
    index_path = os.path.join(date_dir, "frame_index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(frame_index, f, ensure_ascii=False, indent=2)

    return {"sheets": sheets, "frame_index_count": len(frame_index)}


# ═══════════════════════════════════════════════════════════
# Stage 4: 视觉分析
# ═══════════════════════════════════════════════════════════

VISION_PROMPT_TEMPLATE = """你是专业的视频内容分析师和剪辑师。请分析这些关键帧截图，为每个视频片段切出多个候选 shots。

用户主题偏好：{theme}

这些截图来自以下视频文件：
{video_list}

每张截图的编号对应关系：编号 1-N 的帧来自第一个视频，以此类推。请根据帧内容推断每段视频中有哪些可用的片段(shots)。

对每个视频文件，找出其中所有有价值的片段，输出一个 JSON 对象，格式如下：

{{
  "shots": [
    {{
      "shot_id": "视频名_s001（如 5894_s001）",
      "source": "**必须使用实际视频文件名**（如 5894_raw.MP4）",
      "start": 起始秒数,
      "end": 结束秒数,
      "duration": 时长秒数,
      "visual_summary": "画面内容描述（20字以内）",
      "shot_types": ["标签1", "标签2"],
      "garden_objects": ["画面中的物体"],
      "actions": ["画面中的动作"],
      "quality": {{
        "clarity": 1-10,
        "stability": 1-10,
        "exposure": 1-10,
        "composition": 1-10
      }},
      "platform_scores": {{
        "hook": 1-10,
        "retention": 1-10,
        "action": 1-10,
        "beauty": 1-10,
        "clarity": 1-10,
        "contrast": 1-10,
        "story_value": 1-10,
        "cover_value": 1-10
      }},
      "recommended_use": ["用途1", "用途2"],
      "delete": false,
      "delete_reason": ""
    }}
  ]
}}

说明：
- shot_types 可选值：全景、中景、特写、跟拍、固定、延时、整理前、整理中、整理后、成果展示、生活气息、动作过程
- recommended_use 可选值：开头、结尾、封面、中段快切、情感高潮、环境交代、细节展示、过渡
- platform_scores: hook=前3秒吸引力, retention=维持停留能力, action=动作变化丰富度, beauty=花草画面美感, clarity=一眼看懂程度, contrast=前后变化对比(乱→整齐), story_value=故事推进价值, cover_value=封面适合度
- delete=true 表示这段素材质量太差不建议使用，需写 delete_reason
- 每个视频至少找出 1-3 个有价值的 shots，宁多勿少

请直接输出 JSON，不要其他文字。"""


def stage_visual_analysis(project_id: str, theme: str = "日常生活记录") -> dict:
    """用多模态模型分析关键帧"""
    import openai

    date_dir = os.path.join(ROOT_DIR, project_id)
    cs_dir = os.path.join(date_dir, "contact_sheets")

    # 找 contact sheets
    sheets = sorted(glob.glob(os.path.join(cs_dir, "*_sheet.jpg")))
    if not sheets:
        # 没有 contact sheet，先生成
        cs_result = stage_contact_sheets(project_id)
        sheets = sorted(glob.glob(os.path.join(cs_dir, "*_sheet.jpg")))
        if not sheets:
            return {"error": "无法生成 contact sheet"}

    update_progress(project_id, "visual_analysis", 10, "构建分析请求...")

    # 获取视频列表
    raw_dir = os.path.join(date_dir, "raw")
    video_info_list = []
    if os.path.isdir(raw_dir):
        for fname in sorted(os.listdir(raw_dir)):
            ext = os.path.splitext(fname)[1].lower()
            if ext in VIDEO_EXTS:
                fpath = os.path.join(raw_dir, fname)
                info = get_video_info(fpath)
                video_info_list.append(f"- {fname}: {info.get('duration', 0):.1f}秒, {info.get('width', 0)}x{info.get('height', 0)}")

    video_list_text = "\n".join(video_info_list) if video_info_list else "(无视频文件信息)"

    # 构建 API 请求
    prompt = VISION_PROMPT_TEMPLATE.format(theme=theme, video_list=video_list_text)
    content = [{"type": "text", "text": prompt}]
    for sp in sheets:
        b64 = image_to_base64(sp)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
        })

    # 优先使用 xiaomi API（支持视觉），fallback 到 OpenAI
    xiaomi_key = os.environ.get("XIAOMI_API_KEY", "")
    xiaomi_base = os.environ.get("XIAOMI_BASE_URL", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base = os.environ.get("OPENAI_BASE_URL", "")

    if xiaomi_key and xiaomi_base:
        api_key = xiaomi_key
        base_url = xiaomi_base
        model = os.environ.get("VISION_MODEL", "mimo-v2.5")
    elif openai_key:
        api_key = openai_key
        base_url = openai_base
        model = os.environ.get("VISION_MODEL", "gpt-4o")
    else:
        api_key = ""
        base_url = ""
        model = "mimo-v2.5"

    update_progress(project_id, "visual_analysis", 30, f"调用 {model} 分析画面...")

    for attempt in range(2):  # 最多重试 1 次
        try:
            client_kwargs = {}
            if api_key:
                client_kwargs["api_key"] = api_key
            if base_url:
                client_kwargs["base_url"] = base_url

            client = openai.OpenAI(**client_kwargs)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=8192,
                temperature=0.2,
            )
            text = response.choices[0].message.content.strip()

            # 提取 JSON
            parsed = extract_json_from_text(text)
            if parsed is None:
                shots = []
            elif isinstance(parsed, dict) and "shots" in parsed:
                shots = parsed["shots"]
            elif isinstance(parsed, list):
                shots = parsed
            else:
                shots = [parsed]

            # 校验：如果解析出的 shots 数量合理，跳出重试
            if shots and len(shots) > 0:
                break
            elif attempt == 0:
                print(f"  解析结果为空，重试...")
                continue

        except Exception as e:
            print(f"  Vision API 错误 (attempt {attempt+1}): {e}")
            if attempt == 1:
                update_progress(project_id, "visual_analysis", 80, f"API 调用失败: {e}")
                shots = _fallback_visual_analysis(project_id)

    # Pydantic 校验
    shots = validate_shots(shots)

    # 确保每个 shot 有 shot_id
    for i, shot in enumerate(shots):
        if not shot.get("shot_id"):
            src = os.path.splitext(shot.get("source", f"unknown"))[0]
            shot["shot_id"] = f"{src}_s{i+1:03d}"

    # 保存 shots.json
    shots_data = {
        "project_id": project_id,
        "shots": shots,
    }
    atomic_write_json(os.path.join(date_dir, "shots.json"), shots_data)

    # 向后兼容：同时生成 analysis.json（扁平化，每个 source 取最佳 shot）
    analysis = _shots_to_analysis(shots)
    atomic_write_json(os.path.join(date_dir, "analysis.json"), analysis)

    update_progress(project_id, "visual_analysis", 100, f"视觉分析完成: {len(shots)} 个 shots")
    return {"shots": shots, "count": len(shots)}



def _shots_to_analysis(shots: list[dict]) -> list[dict]:
    """将 shots 数据转换为旧版 analysis 格式（每个 source 取最佳 shot）"""
    by_source = {}
    for shot in shots:
        src = shot.get("source", "")
        if not src:
            continue
        # 用 platform_scores 的平均值作为评分
        ps = shot.get("platform_scores", {})
        score = sum(ps.values()) / max(len(ps), 1) if ps else 5
        if src not in by_source or score > by_source[src]["_score"]:
            q = shot.get("quality", {})
            by_source[src] = {
                "file": src,
                "start": shot.get("start", 0),
                "end": shot.get("end", 0),
                "duration": shot.get("duration", 0),
                "action": shot.get("visual_summary", ""),
                "subjects": shot.get("garden_objects", []),
                "story_role": _use_to_role(shot.get("recommended_use", [])),
                "quality_score": round(sum(q.values()) / max(len(q), 1)) if q else 6,
                "stability": q.get("stability", 7),
                "privacy_risk": 1,
                "caption": shot.get("visual_summary", ""),
                "_score": score,
            }
    # 清理临时评分字段
    for seg in by_source.values():
        seg.pop("_score", None)
    return list(by_source.values())


def _use_to_role(uses: list[str]) -> str:
    """将 recommended_use 映射到 story_role"""
    mapping = {
        "开头": "opening",
        "结尾": "ending",
        "封面": "result",
        "环境交代": "space",
        "中段快切": "action",
        "情感高潮": "result",
        "细节展示": "detail",
        "过渡": "space",
    }
    for use in uses:
        if use in mapping:
            return mapping[use]
    return "action"


def _role_to_use(role: str) -> str:
    """将 story_role 映射到 recommended_use"""
    mapping = {
        "opening": "开头",
        "ending": "结尾",
        "result": "封面",
        "space": "环境交代",
        "action": "中段快切",
        "action_intro": "中段快切",
        "collect": "中段快切",
        "life": "情感高潮",
        "detail": "细节展示",
    }
    return mapping.get(role, "中段快切")


def stage_select_template(project_id: str) -> dict:
    """根据 shots 数据判断最适合的视频模板"""
    date_dir = os.path.join(ROOT_DIR, project_id)

    # 读取 shots
    shots_path = os.path.join(date_dir, "shots.json")
    shots = []
    if os.path.exists(shots_path):
        with open(shots_path, encoding="utf-8") as f:
            shots = json.load(f).get("shots", [])
    else:
        analysis_path = os.path.join(date_dir, "analysis.json")
        if os.path.exists(analysis_path):
            with open(analysis_path, encoding="utf-8") as f:
                shots = json.load(f)

    if not shots:
        return {"video_template": "one_problem", "template_name": "一个问题解决型", "reason": "无分析数据，默认模板"}

    # 统计 shot_types 和 actions
    all_types = []
    all_actions = []
    all_objects = []
    beauty_scores = []
    action_count = 0
    has_before = False
    has_after = False
    has_core_action = False

    for shot in shots:
        if shot.get("delete"):
            continue
        all_types.extend(shot.get("shot_types", []))
        all_actions.extend(shot.get("actions", []))
        all_objects.extend(shot.get("garden_objects", []))

        ps = shot.get("platform_scores", {})
        beauty_scores.append(ps.get("beauty", 5))
        if ps.get("action", 0) >= 6:
            action_count += 1

        # 检查前后对比特征
        for t in shot.get("shot_types", []):
            if "整理前" in t or "乱" in t:
                has_before = True
            if "整理后" in t or "成果" in t:
                has_after = True
            if "核心动作" in t or "修剪" in t or "整理中" in t:
                has_core_action = True

    avg_beauty = sum(beauty_scores) / max(len(beauty_scores), 1)
    all_text = " ".join(all_types + all_actions + all_objects)

    # 判断模板（按优先级）
    if has_before and has_core_action and has_after:
        template_key = "before_after"
        reason = f"素材中包含整理前、核心动作、整理后，适合前后对比"
    elif any(k in all_text for k in ["花苗", "种子", "栽种", "种植", "施肥", "浇水", "养护"]):
        template_key = "tutorial"
        reason = f"素材包含种植/养护相关内容，适合教程"
    elif action_count >= 3:
        template_key = "garden_diary"
        reason = f"有 {action_count} 个动作片段，适合花园日记"
    elif avg_beauty >= 8 and action_count <= 1:
        template_key = "healing_mood"
        reason = f"画面平均美感 {avg_beauty:.0f}/10 且动作少，适合治愈氛围片"
    else:
        template_key = "one_problem"
        reason = f"默认使用问题解决型模板"

    template = VIDEO_TEMPLATES[template_key]
    result = {
        "video_template": template_key,
        "template_name": template["name"],
        "reason": reason,
        "structure": template["structure"],
        "tone": template["tone"],
        "hook_strategy": template["hook_strategy"],
    }

    # 保存
    template_path = os.path.join(date_dir, "video_template.json")
    with open(template_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    update_progress(project_id, "template_select", 100, f"模板选择: {template['name']}")
    return result


def _fallback_visual_analysis(project_id: str) -> list[dict]:
    """无 API 时的 fallback 分析 — 返回 shots 格式"""
    raw_dir = os.path.join(ROOT_DIR, project_id, "raw")
    results = []
    if not os.path.isdir(raw_dir):
        return results
    for fname in sorted(os.listdir(raw_dir)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTS:
            continue
        fpath = os.path.join(raw_dir, fname)
        info = get_video_info(fpath)
        dur = info.get("duration", 0)
        base = os.path.splitext(fname)[0]
        results.append({
            "shot_id": f"{base}_s001",
            "source": fname,
            "start": 0.0,
            "end": min(dur, 15.0),
            "duration": min(dur, 15.0),
            "visual_summary": "待分析",
            "shot_types": [],
            "garden_objects": [],
            "actions": [],
            "quality": {"clarity": 6, "stability": 7, "exposure": 6, "composition": 6},
            "platform_scores": {"hook": 5, "retention": 5, "action": 3, "beauty": 5, "clarity": 6, "contrast": 4, "story_value": 5, "cover_value": 4},
            "recommended_use": ["中段快切"],
            "delete": False,
            "delete_reason": "",
        })
    return results


# ═══════════════════════════════════════════════════════════
# Stage 5: 音频转字幕 (Whisper)
# ═══════════════════════════════════════════════════════════

def stage_transcription(project_id: str) -> dict:
    """提取音频并用 Whisper 转写为字幕"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    raw_dir = os.path.join(date_dir, "raw")
    if not os.path.isdir(raw_dir):
        return {"error": "raw 目录不存在"}

    videos = sorted([f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS])
    audio_dir = ensure_dir(date_dir, "audio")

    transcriptions = []

    for i, fname in enumerate(videos):
        vpath = os.path.join(raw_dir, fname)
        base = os.path.splitext(fname)[0]
        audio_path = os.path.join(audio_dir, f"{base}.wav")
        pct = int((i / len(videos)) * 30)
        update_progress(project_id, "transcription", pct, f"提取音频: {fname}")

        # 提取音频
        run_cmd([
            "ffmpeg", "-y", "-i", vpath,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path
        ], check=False, timeout=300)

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            transcriptions.append({
                "file": fname,
                "language": "unknown",
                "segments": [],
                "text": "",
                "note": "无音频或音频过短"
            })
            continue

        # Whisper 转写
        update_progress(project_id, "transcription", pct + 15, f"Whisper 转写: {fname}")
        whisper_result = _whisper_transcribe(audio_path)

        transcriptions.append({
            "file": fname,
            **whisper_result,
        })

    # 合并字幕
    all_segments = []
    for t in transcriptions:
        all_segments.extend(t.get("segments", []))

    # 保存
    transcription_path = os.path.join(date_dir, "transcription.json")
    with open(transcription_path, "w", encoding="utf-8") as f:
        json.dump(transcriptions, f, ensure_ascii=False, indent=2)

    # 生成 SRT
    srt_path = os.path.join(date_dir, "raw_captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(all_segments, 1):
            f.write(f"{i}\n")
            f.write(f"{seconds_to_srt(seg['start'])} --> {seconds_to_srt(seg['end'])}\n")
            f.write(f"{seg['text']}\n\n")

    update_progress(project_id, "transcription", 100, f"转写完成: {len(transcriptions)} 个视频, {len(all_segments)} 条字幕")
    return {"transcriptions": transcriptions, "segment_count": len(all_segments)}


def _whisper_transcribe(audio_path: str) -> dict:
    """Whisper 转写 - 优先本地，fallback 到 API"""
    # 方法1: 本地 whisper（更快、免费）
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, language="zh", verbose=False)
        segments = [{
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
        } for seg in result.get("segments", [])]
        return {
            "language": result.get("language", "zh"),
            "segments": segments,
            "text": " ".join(s["text"] for s in segments),
        }
    except Exception as e:
        print(f"  本地 whisper 错误: {e}")

    # 方法2: OpenAI Whisper API (fallback)
    xiaomi_key = os.environ.get("XIAOMI_API_KEY", "")
    xiaomi_base = os.environ.get("XIAOMI_BASE_URL", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base = os.environ.get("OPENAI_BASE_URL", "")

    api_key = openai_key or xiaomi_key
    base_url = openai_base or ""

    if api_key:
        try:
            import openai
            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url

            client = openai.OpenAI(**client_kwargs)
            with open(audio_path, "rb") as audio_file:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json",
                    language="zh",
                )

            segments = []
            if hasattr(result, 'segments') and result.segments:
                for seg in result.segments:
                    segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
            else:
                segments.append({"start": 0.0, "end": 10.0, "text": result.text if hasattr(result, 'text') else str(result)})

            return {
                "language": getattr(result, 'language', 'zh'),
                "segments": segments,
                "text": " ".join(s["text"] for s in segments),
            }
        except Exception as e:
            print(f"  Whisper API 错误: {e}")

    return {"language": "unknown", "segments": [], "text": "", "note": "Whisper 不可用"}


# ═══════════════════════════════════════════════════════════
# Stage 6: 剧情脚本生成
# ═══════════════════════════════════════════════════════════

STORY_PROMPT_TEMPLATE = """你是一位短视频编剧，擅长 60 秒竖屏生活记录短片。

## 任务
根据以下素材信息，编写一个完整的短视频剧情脚本。

## 用户主题偏好
{theme}

## 可用素材（视觉分析结果）
{materials}

## 音频转写内容
{transcription_text}

## 可用视频文件及时长
{video_list}

## 要求
1. 视频时长 55-75 秒，9:16 竖屏
2. 故事结构: 开场吸引 → 内容展开 → 情感高潮 → 余韵收尾
3. 字幕要简短有力（15字以内），有文采但不做作
4. 考虑画面质量、隐私风险来选择素材
5. 配音建议（旁白 / 纯音乐 / 保留原声）
6. **source 字段必须使用实际视频文件名**（如 5894_raw.MP4），不要使用 frame_set 或关键帧文件名

## 输出 JSON 格式
{{
  "title": "视频标题",
  "subtitle": "副标题/一句话描述",
  "storyline": "一句话故事线",
  "tone": "整体调性（如：清新治愈、热血励志等）",
  "duration_target": {target_duration},
  "music_style": "背景音乐建议",
  "voiceover": "是否需要旁白（true/false）",
  "clips": [
    {{
      "id": "001_opening",
      "role": "opening",
      "source": "源视频文件名（如5902_raw.MP4）",
      "start": 0.0,
      "end": 3.0,
      "duration": 3.0,
      "caption": "字幕文字",
      "voiceover_text": "旁白文字（如有）",
      "transition": "转场效果",
      "note": "剪辑说明"
    }}
  ],
  "total_duration": 66,
  "ending_caption": "结尾文字",
  "hashtags": ["话题标签1", "话题标签2"]
}}

请直接输出 JSON，不要其他文字。"""


def stage_story_script(project_id: str, theme: str = "日常生活记录", target_duration: int = 60) -> dict:
    """生成剧情脚本"""
    import openai

    date_dir = os.path.join(ROOT_DIR, project_id)
    update_progress(project_id, "story_script", 10, "准备素材信息...")

    # 读取 shots（优先）或 analysis（兼容）
    shots_path = os.path.join(date_dir, "shots.json")
    shots = []
    if os.path.exists(shots_path):
        with open(shots_path, encoding="utf-8") as f:
            shots_data = json.load(f)
            shots = shots_data.get("shots", [])
    else:
        analysis_path = os.path.join(date_dir, "analysis.json")
        if os.path.exists(analysis_path):
            with open(analysis_path, encoding="utf-8") as f:
                analysis = json.load(f)
            # 转换为 shots 格式
            for seg in analysis:
                shots.append({
                    "shot_id": seg.get("file", "unknown") + "_s001",
                    "source": seg.get("file", ""),
                    "start": seg.get("start", 0),
                    "end": seg.get("end", 0),
                    "duration": seg.get("duration", 0),
                    "visual_summary": seg.get("action", ""),
                    "shot_types": [],
                    "garden_objects": seg.get("subjects", []),
                    "actions": [],
                    "quality": {"clarity": seg.get("quality_score", 6), "stability": seg.get("stability", 7), "exposure": 6, "composition": 6},
                    "platform_scores": {"hook": 5, "retention": 5, "action": 3, "beauty": seg.get("quality_score", 5), "clarity": 6, "contrast": 4, "story_value": 5, "cover_value": 4},
                    "recommended_use": [_role_to_use(seg.get("story_role", "action"))],
                    "delete": False,
                    "delete_reason": "",
                })

    # 读取转写结果
    transcription_path = os.path.join(date_dir, "transcription.json")
    transcriptions = []
    if os.path.exists(transcription_path):
        with open(transcription_path, encoding="utf-8") as f:
            transcriptions = json.load(f)

    # 构建素材描述 — 按推荐用途分组
    materials = ""
    by_use = {"开头": [], "结尾": [], "封面": [], "中段快切": [], "情感高潮": [], "环境交代": [], "细节展示": [], "过渡": []}
    for shot in shots:
        if shot.get("delete"):
            continue
        for use in shot.get("recommended_use", []):
            if use in by_use:
                by_use[use].append(shot)

    for use, use_shots in by_use.items():
        if use_shots:
            materials += f"\n## 推荐做「{use}」的素材:\n"
            for s in use_shots:
                ps = s.get("platform_scores", {})
                q = s.get("quality", {})
                materials += (f"- [{s.get('shot_id', '?')}] {s.get('source', '?')} "
                              f"({s.get('start', 0):.1f}-{s.get('end', 0):.1f}s): "
                              f"{s.get('visual_summary', '?')} | "
                              f"类型:{','.join(s.get('shot_types', []))} | "
                              f"质量:{sum(q.values())/max(len(q),1):.0f}/10 "
                              f"hook:{ps.get('hook',5)} retention:{ps.get('retention',5)} "
                              f"action:{ps.get('action',5)} story:{ps.get('story_value',5)}\n")

    # 未分类的 shots
    uncategorized = [s for s in shots if not s.get("delete") and not any(
        use in by_use for use in s.get("recommended_use", []))]
    if uncategorized:
        materials += "\n## 其他素材:\n"
        for s in uncategorized:
            materials += f"- [{s.get('shot_id', '?')}] {s.get('source', '?')} ({s.get('start', 0):.1f}-{s.get('end', 0):.1f}s): {s.get('visual_summary', '?')}\n"

    # 构建转写文本
    transcription_text = ""
    for t in transcriptions:
        if t.get("text"):
            transcription_text += f"\n[{t['file']}]: {t['text']}"
    if not transcription_text:
        transcription_text = "(无音频转写内容)"

    update_progress(project_id, "story_script", 30, "调用 AI 生成脚本...")

    # 获取视频列表
    raw_dir = os.path.join(date_dir, "raw")
    video_list_text = ""
    if os.path.isdir(raw_dir):
        for fname in sorted(os.listdir(raw_dir)):
            if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                fpath = os.path.join(raw_dir, fname)
                info = get_video_info(fpath)
                video_list_text += f"- {fname}: {info.get('duration', 0):.1f}秒 {'(有音频)' if info.get('has_audio') else '(无音频)'}\n"

    prompt = STORY_PROMPT_TEMPLATE.format(
        theme=theme,
        materials=materials or "(无视觉分析数据)",
        transcription_text=transcription_text,
        video_list=video_list_text or "(无视频文件)",
        target_duration=target_duration,
    )

    # 调用 API
    # 优先使用 xiaomi API
    xiaomi_key = os.environ.get("XIAOMI_API_KEY", "")
    xiaomi_base = os.environ.get("XIAOMI_BASE_URL", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base = os.environ.get("OPENAI_BASE_URL", "")

    if xiaomi_key and xiaomi_base:
        api_key = xiaomi_key
        base_url = xiaomi_base
        model = os.environ.get("STORY_MODEL", "mimo-v2.5-pro")
    elif openai_key:
        api_key = openai_key
        base_url = openai_base
        model = os.environ.get("STORY_MODEL", "gpt-4o")
    else:
        api_key = ""
        base_url = ""
        model = "mimo-v2.5-pro"

    try:
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url

        client = openai.OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.7,
        )
        text = response.choices[0].message.content.strip()

        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            script = json.loads(json_match.group())
        else:
            script = _fallback_story_script(project_id, theme)
    except Exception as e:
        print(f"  故事脚本 API 错误: {e}")
        script = _fallback_story_script(project_id, theme)

    # 补充源文件路径
    if "clips" in script:
        for clip in script["clips"]:
            src = clip.get("source", "")
            if src:
                src_path = os.path.join(raw_dir, src)
                if not os.path.exists(src_path):
                    # 模糊匹配
                    matches = glob.glob(os.path.join(raw_dir, f"*{src[:6]}*"))
                    if matches:
                        clip["source"] = os.path.basename(matches[0])

    # 保存
    script_path = os.path.join(date_dir, "story_script.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    update_progress(project_id, "story_script", 100, f"脚本生成完成: {script.get('title', '未命名')}")
    return {"script": script}


def _fallback_story_script(project_id: str, theme: str) -> dict:
    """无 API 时的 fallback 脚本"""
    raw_dir = os.path.join(ROOT_DIR, project_id, "raw")
    videos = []
    if os.path.isdir(raw_dir):
        videos = sorted([f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS])

    clips = []
    for i, fname in enumerate(videos):
        fpath = os.path.join(raw_dir, fname)
        info = get_video_info(fpath)
        dur = info.get("duration", 10)
        role = STORY_SLOTS[i % len(STORY_SLOTS)]["role"] if i < len(STORY_SLOTS) else "action"
        clips.append({
            "id": f"{i+1:03d}_{role}",
            "role": role,
            "source": fname,
            "start": 0.0,
            "end": min(dur, 10.0),
            "duration": min(dur, 10.0),
            "caption": f"{role} 片段",
            "voiceover_text": "",
            "transition": "cut",
            "note": "",
        })

    return {
        "title": f"{theme}记录",
        "subtitle": "日常生活的美好瞬间",
        "storyline": f"记录{theme}的过程",
        "tone": "清新自然",
        "duration_target": 66,
        "music_style": "轻音乐",
        "voiceover": False,
        "clips": clips,
        "total_duration": sum(c["duration"] for c in clips),
        "ending_caption": "今天就到这里。",
        "hashtags": ["生活记录", "日常"],
    }


# ═══════════════════════════════════════════════════════════
# Stage 7: 视频渲染
# ═══════════════════════════════════════════════════════════

def stage_render(project_id: str, burn_subtitles: bool = True, audio_mode: str = "source",
                 mode: str = "publish", show_debug_overlay: bool = False) -> dict:
    """渲染最终视频
    mode: "draft" (720p快速预览) 或 "publish" (1080p正式版)
    show_debug_overlay: 草稿模式下是否显示调试信息
    """
    date_dir = os.path.join(ROOT_DIR, project_id)
    raw_dir = os.path.join(date_dir, "raw")
    out_dir = ensure_dir(date_dir, "outputs")
    temp_dir = ensure_dir(out_dir, "_temp_clips")

    # 读取故事脚本或编辑计划
    script_path = os.path.join(date_dir, "story_script.json")
    plan_path = os.path.join(date_dir, "edit_plan.json")

    if os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            plan_data = json.load(f)
        clips = plan_data.get("clips", [])
        title = plan_data.get("title", "")
    elif os.path.exists(plan_path):
        with open(plan_path, encoding="utf-8") as f:
            plan_data = json.load(f)
        clips = plan_data.get("clips", [])
        title = plan_data.get("title", "")
    else:
        return {"error": "没有剪辑计划或故事脚本"}

    if not clips:
        return {"error": "没有可渲染的片段"}

    is_draft = mode == "draft"
    if is_draft:
        show_debug_overlay = True

    update_progress(project_id, "render", 5, f"{'草稿' if is_draft else '发布'}模式渲染 {len(clips)} 个片段...")

    spec = DRAFT_SPEC if is_draft else OUTPUT_SPEC
    w, h, fps = spec["width"], spec["height"], spec["fps"]
    crf, preset = spec["crf"], spec["preset"]
    sr = spec["audio_sample_rate"]

    temp_clips = []
    srt_entries = []
    timeline_pos = 0.0

    for i, clip in enumerate(clips):
        src_name = clip.get("source", "")
        src_path = os.path.join(raw_dir, src_name)

        # 模糊匹配
        if not os.path.exists(src_path):
            matches = glob.glob(os.path.join(raw_dir, f"*{src_name[:8]}*"))
            if matches:
                src_path = matches[0]
            else:
                print(f"  ⚠ 找不到源文件: {src_name}")
                continue

        start = float(clip.get("start", 0))
        duration = float(clip.get("duration", clip.get("end", 10) - start))
        end = start + duration
        has_audio = get_video_info(src_path).get("has_audio", True)
        pct = int((i / len(clips)) * 80)
        update_progress(project_id, "render", pct, f"渲染片段 {i+1}/{len(clips)}: {clip.get('role', '?')}")

        out_clip = os.path.join(temp_dir, f"{i+1:03d}_{clip.get('role', 'clip')}.mp4")

        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p")

        # 草稿模式：叠加调试信息
        if show_debug_overlay:
            role = clip.get('role', '?')
            caption = clip.get('caption', '')[:20]
            debug_text = f"#{i+1} {role} {start:.1f}-{end:.1f}s {caption}"
            # 转义 ffmpeg drawtext 特殊字符
            debug_text = debug_text.replace("'", "\\'").replace(":", "\\:")
            vf += (f",drawtext=text='{debug_text}'"
                   f":fontsize=18:fontcolor=white:borderw=2:bordercolor=black"
                   f":x=10:y=10")
            # 时间码
            vf += (f",drawtext=text='%{{pts\\:hms}}'"
                   f":fontsize=14:fontcolor=yellow:borderw=1:bordercolor=black"
                   f":x=10:y=40")

        if audio_mode == "source" and has_audio:
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-i", src_path,
                "-map", "0:v:0", "-map", "0:a:0",
                "-vf", vf, "-r", str(fps),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-c:a", "aac", "-ar", str(sr), "-ac", "2",
                "-movflags", "+faststart",
                out_clip,
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-i", src_path,
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={sr}",
                "-map", "0:v:0", "-map", "1:a:0",
                "-vf", vf, "-r", str(fps),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-c:a", "aac", "-ar", str(sr), "-ac", "2",
                "-shortest", "-movflags", "+faststart",
                out_clip,
            ]

        run_cmd(cmd, check=False, timeout=300)

        if os.path.exists(out_clip) and os.path.getsize(out_clip) > 1000:
            temp_clips.append(out_clip)
            caption = clip.get("caption", "")
            if caption:
                srt_entries.append({
                    "index": len(srt_entries) + 1,
                    "start": timeline_pos,
                    "end": timeline_pos + duration,
                    "text": caption,
                })
            timeline_pos += duration

    if not temp_clips:
        return {"error": "没有成功渲染的片段"}

    # 合并片段 — 先 ffprobe 校验所有临时片段一致性
    update_progress(project_id, "render", 85, "校验片段一致性...")

    # 收集每个片段的流信息
    clip_streams = []
    for cp in temp_clips:
        try:
            r = run_cmd([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", cp
            ], check=False, timeout=10)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
                aus = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
                clip_streams.append({
                    "path": cp,
                    "width": vs.get("width"),
                    "height": vs.get("height"),
                    "r_frame_rate": vs.get("r_frame_rate"),
                    "codec_name": vs.get("codec_name"),
                    "pix_fmt": vs.get("pix_fmt"),
                    "sample_rate": aus.get("sample_rate"),
                    "channel_layout": aus.get("channel_layout"),
                })
            else:
                clip_streams.append({"path": cp, "error": "ffprobe 失败"})
        except Exception as e:
            clip_streams.append({"path": cp, "error": str(e)})

    # 检查一致性
    if clip_streams and all("error" not in s for s in clip_streams):
        ref = clip_streams[0]
        inconsistent = []
        for cs in clip_streams[1:]:
            mismatches = []
            for key in ["width", "height", "r_frame_rate", "codec_name", "pix_fmt", "sample_rate"]:
                if cs.get(key) != ref.get(key):
                    mismatches.append(f"{key}: {cs.get(key)} vs {ref.get(key)}")
            if mismatches:
                inconsistent.append({"path": cs["path"], "mismatches": mismatches})

        if inconsistent:
            print(f"  ⚠ 发现 {len(inconsistent)} 个片段格式不一致，将重新转码")
            for inc in inconsistent:
                print(f"    {os.path.basename(inc['path'])}: {', '.join(inc['mismatches'])}")
            # 重新转码不一致的片段
            for inc in inconsistent:
                reencode_path = inc["path"].replace(".mp4", "_re.mp4")
                vf_re = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                         f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p")
                run_cmd([
                    "ffmpeg", "-y", "-i", inc["path"],
                    "-vf", vf_re, "-r", str(fps),
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-c:a", "aac", "-ar", str(sr), "-ac", "2",
                    "-movflags", "+faststart",
                    reencode_path,
                ], check=False, timeout=300)
                if os.path.exists(reencode_path) and os.path.getsize(reencode_path) > 1000:
                    # 替换原片段
                    idx = temp_clips.index(inc["path"])
                    temp_clips[idx] = reencode_path
                    print(f"    ✓ 已重新转码: {os.path.basename(reencode_path)}")

    # 写入 concat 列表并合并
    update_progress(project_id, "render", 88, "合并片段...")
    concat_file = os.path.join(out_dir, "_concat.txt")
    with open(concat_file, "w") as f:
        for clip_path in temp_clips:
            escaped = str(os.path.abspath(clip_path)).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    output_name = "draft_cut.mp4" if is_draft else "rough_cut.mp4"
    rough_cut = os.path.join(out_dir, output_name)
    run_cmd([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        "-movflags", "+faststart",
        rough_cut,
    ], check=False, timeout=300)

    current_output = rough_cut

    # 生成字幕文件
    srt_path = os.path.join(out_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for entry in srt_entries:
            f.write(f"{entry['index']}\n")
            f.write(f"{seconds_to_srt(entry['start'])} --> {seconds_to_srt(entry['end'])}\n")
            f.write(f"{entry['text']}\n\n")

    # 烧录字幕
    if burn_subtitles and srt_entries:
        update_progress(project_id, "render", 90, "烧录字幕...")
        final_sub = os.path.join(out_dir, "final_with_subtitles.mp4")
        srt_escaped = str(os.path.abspath(srt_path)).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        vf_sub = (f"subtitles='{srt_escaped}':"
                  f"force_style='FontName=PingFang SC,FontSize=14,Alignment=2,MarginV=120,Outline=2'")
        run_cmd([
            "ffmpeg", "-y",
            "-i", rough_cut,
            "-vf", vf_sub,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            final_sub,
        ], check=False, timeout=300)
        if os.path.exists(final_sub):
            current_output = final_sub

    # 生成封面
    update_progress(project_id, "render", 95, "生成封面...")
    cover_path = os.path.join(out_dir, "cover.jpg")
    mid_point = timeline_pos / 2
    run_cmd([
        "ffmpeg", "-y",
        "-ss", f"{mid_point:.1f}",
        "-i", rough_cut,
        "-vframes", "1",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
        cover_path,
    ], check=False)

    # 清理临时文件
    for clip_path in temp_clips:
        try:
            os.remove(clip_path)
        except:
            pass
    try:
        os.remove(concat_file)
        os.rmdir(temp_dir)
    except:
        pass

    # 保存渲染信息
    render_info = {
        "output": current_output,
        "rough_cut": rough_cut,
        "srt": srt_path,
        "cover": cover_path if os.path.exists(cover_path) else None,
        "title": title,
        "duration": timeline_pos,
        "clip_count": len(temp_clips),
        "subtitle_count": len(srt_entries),
        "rendered_at": datetime.datetime.now().isoformat(),
    }
    info_path = os.path.join(out_dir, "render_info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(render_info, f, ensure_ascii=False, indent=2)

    update_progress(project_id, "render", 100, f"渲染完成! 时长 {timeline_pos:.1f}秒, {len(temp_clips)} 个片段")
    return render_info


# ═══════════════════════════════════════════════════════════
# Stage 8: 生成编辑计划 (从故事脚本)
# ═══════════════════════════════════════════════════════════

def stage_edit_plan(project_id: str, theme: str = "日常生活记录") -> dict:
    """从故事脚本生成可渲染的编辑计划"""
    date_dir = os.path.join(ROOT_DIR, project_id)

    # 如果已有故事脚本，直接转换
    script_path = os.path.join(date_dir, "story_script.json")
    if os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            script = json.load(f)

        raw_dir = os.path.join(date_dir, "raw")
        clips = script.get("clips", [])
        enriched_clips = []
        timeline_pos = 0.0

        for clip in clips:
            src_name = clip.get("source", "")
            src_path = os.path.join(raw_dir, src_name)

            # 模糊匹配
            if not os.path.exists(src_path):
                matches = glob.glob(os.path.join(raw_dir, f"*{src_name[:8]}*"))
                if matches:
                    src_path = matches[0]
                    src_name = os.path.basename(matches[0])

            # 如果仍然找不到（如 frame_set_X.jpg），跳过
            if not os.path.exists(src_path):
                # 尝试用分析数据中的时间信息找到最佳替代视频
                analysis_path = os.path.join(date_dir, "analysis.json")
                if os.path.exists(analysis_path):
                    with open(analysis_path, encoding="utf-8") as af:
                        analysis = json.load(af)
                    # 找到对应的分析片段，用其 file 字段
                    for a_seg in analysis:
                        if a_seg.get("file") == src_name:
                            alt_name = a_seg.get("file", "")
                            alt_path = os.path.join(raw_dir, alt_name)
                            if os.path.exists(alt_path):
                                src_path = alt_path
                                src_name = alt_name
                                break
                # 最后 fallback: 用第一个可用视频
                if not os.path.exists(src_path):
                    available = [f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
                    if available:
                        src_path = os.path.join(raw_dir, available[0])
                        src_name = available[0]
                    else:
                        continue

            info = get_video_info(src_path)
            dur = float(clip.get("duration", clip.get("end", 10) - clip.get("start", 0)))

            enriched_clips.append({
                **clip,
                "source": src_name,
                "source_path": src_path,
                "source_duration": info.get("duration", 0),
                "source_has_audio": info.get("has_audio", False),
                "timeline_start": timeline_pos,
                "timeline_end": timeline_pos + dur,
            })
            timeline_pos += dur

        plan = {
            "version": "story-script-v1",
            "created_at": datetime.datetime.now().isoformat(),
            "title": script.get("title", ""),
            "subtitle": script.get("subtitle", ""),
            "storyline": script.get("storyline", ""),
            "tone": script.get("tone", ""),
            "hashtags": script.get("hashtags", []),
            "raw_dir": raw_dir,
            "target_duration": script.get("duration_target", 66),
            "actual_duration": timeline_pos,
            "render": OUTPUT_SPEC,
            "clips": enriched_clips,
        }

        plan_path = os.path.join(date_dir, "edit_plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        return {"plan": plan}

    # 没有故事脚本，从 shots 或 analysis 结果生成
    shots_path = os.path.join(date_dir, "shots.json")
    shots = []
    if os.path.exists(shots_path):
        with open(shots_path, encoding="utf-8") as f:
            shots_data = json.load(f)
            shots = shots_data.get("shots", [])
    else:
        analysis_path = os.path.join(date_dir, "analysis.json")
        if os.path.exists(analysis_path):
            with open(analysis_path, encoding="utf-8") as f:
                analysis = json.load(f)
            # 转换为 shots 格式
            for seg in analysis:
                shots.append({
                    "shot_id": seg.get("file", "unknown") + "_s001",
                    "source": seg.get("file", ""),
                    "start": seg.get("start", 0),
                    "end": seg.get("end", 0),
                    "duration": seg.get("duration", 0),
                    "visual_summary": seg.get("action", ""),
                    "shot_types": [],
                    "garden_objects": seg.get("subjects", []),
                    "actions": [],
                    "quality": {"clarity": seg.get("quality_score", 6), "stability": seg.get("stability", 7), "exposure": 6, "composition": 6},
                    "platform_scores": {"hook": 5, "retention": 5, "action": 3, "beauty": seg.get("quality_score", 5), "clarity": 6, "contrast": 4, "story_value": 5, "cover_value": 4},
                    "recommended_use": [_role_to_use(seg.get("story_role", "action"))],
                    "delete": False,
                    "delete_reason": "",
                })

    if not shots:
        return {"error": "没有故事脚本或分析结果"}

    # 评分排序 — 使用 platform_scores 加权
    for shot in shots:
        ps = shot.get("platform_scores", {})
        q = shot.get("quality", {})
        # 综合评分：platform 加权 + 质量加权（花园号：动作、停留、故事 > 单纯好看）
        platform_score = (
            ps.get("hook", 5) * 0.18 +
            ps.get("retention", 5) * 0.18 +
            ps.get("action", 5) * 0.16 +
            ps.get("story_value", 5) * 0.16 +
            ps.get("beauty", 5) * 0.12 +
            ps.get("clarity", 5) * 0.10 +
            ps.get("contrast", 5) * 0.07 +
            ps.get("cover_value", 5) * 0.03
        )
        quality_score = sum(q.values()) / max(len(q), 1) if q else 6
        shot["_score"] = platform_score * 0.6 + quality_score * 0.4
        # 跳过标记删除的
        if shot.get("delete"):
            shot["_score"] = -1

    # 按 story slot 的推荐用途匹配最佳 shots
    selected = []
    used_sources = set()

    for slot in STORY_SLOTS:
        role = slot["role"]
        use = _role_to_use(role)
        # 找推荐用途匹配的 shots
        candidates = [s for s in shots
                      if not s.get("delete")
                      and use in s.get("recommended_use", [])
                      and s.get("source") not in used_sources]
        if not candidates:
            # 退而求其次，找 story_role 匹配的
            candidates = [s for s in shots
                          if not s.get("delete")
                          and s.get("source") not in used_sources]
        if candidates:
            candidates.sort(key=lambda s: s["_score"], reverse=True)
            chosen = candidates[0]
            selected.append(chosen)
            used_sources.add(chosen.get("source", ""))

    # 填充不足
    if len(selected) < 4:
        for shot in sorted(shots, key=lambda s: s["_score"], reverse=True):
            if len(selected) >= len(STORY_SLOTS):
                break
            if shot.get("delete"):
                continue
            if shot.get("source") not in used_sources:
                selected.append(shot)
                used_sources.add(shot.get("source", ""))

    raw_dir = os.path.join(date_dir, "raw")
    plan_clips = []
    timeline_pos = 0.0
    for i, shot in enumerate(selected):
        src_name = shot.get("source", "")
        src_path = os.path.join(raw_dir, src_name)
        if not os.path.exists(src_path):
            continue
        info = get_video_info(src_path)
        dur = float(shot.get("duration", shot.get("end", 10) - shot.get("start", 0)))
        role = STORY_SLOTS[i % len(STORY_SLOTS)]["role"] if i < len(STORY_SLOTS) else "action"

        # 剪辑意图：基于 shot 数据自动生成
        ps = shot.get("platform_scores", {})
        q = shot.get("quality", {})
        uses = shot.get("recommended_use", [])
        avg_quality = sum(q.values()) / max(len(q), 1) if q else 6

        # why_selected: 为什么选这个 shot
        why_parts = []
        if ps.get("hook", 0) >= 7:
            why_parts.append("开头吸引力强")
        if ps.get("action", 0) >= 7:
            why_parts.append("动作丰富")
        if ps.get("beauty", 0) >= 7:
            why_parts.append("画面美感好")
        if ps.get("story_value", 0) >= 7:
            why_parts.append("推进故事")
        if ps.get("contrast", 0) >= 7:
            why_parts.append("有前后对比")
        if not why_parts:
            why_parts.append(f"综合评分 {avg_quality:.0f}/10")
        why_selected = f"{shot.get('visual_summary', '')}，{'、'.join(why_parts)}"

        # risk: 潜在风险
        risk = ""
        if q.get("exposure", 10) <= 4:
            risk = "曝光不足，建议适当提亮"
        elif q.get("stability", 10) <= 4:
            risk = "画面抖动，建议加稳定"
        elif q.get("clarity", 10) <= 4:
            risk = "画面模糊，建议缩短使用时长"

        # platform_goal: 这个片段在平台上的目标
        platform_goal = "提升整体节奏"
        if "开头" in uses:
            platform_goal = "前3秒抓住注意力"
        elif "结尾" in uses:
            platform_goal = "留有余韵，引导互动"
        elif "封面" in uses:
            platform_goal = "吸引点击"
        elif "中段快切" in uses:
            platform_goal = "维持观看节奏"
        elif "情感高潮" in uses:
            platform_goal = "引发共鸣"
        elif "细节展示" in uses:
            platform_goal = "展示精致细节"

        # edit_style: 剪辑风格
        edit_style = "normal"
        if ps.get("action", 0) >= 8:
            edit_style = "fast_cut"
        elif ps.get("beauty", 0) >= 8 and ps.get("action", 0) <= 4:
            edit_style = "slow_motion"
        elif role in ("opening", "ending"):
            edit_style = "fade"

        plan_clips.append({
            "role": role,
            "source": src_name,
            "source_path": src_path,
            "source_duration": info.get("duration", 0),
            "source_has_audio": info.get("has_audio", False),
            "start": float(shot.get("start", 0)),
            "end": float(shot.get("end", dur)),
            "duration": dur,
            "timeline_start": timeline_pos,
            "timeline_end": timeline_pos + dur,
            "caption": shot.get("visual_summary", ""),
            "note": f"shot: {shot.get('shot_id', '?')}",
            # 剪辑意图
            "edit_style": edit_style,
            "speed": 1.0,
            "why_selected": why_selected,
            "risk": risk,
            "platform_goal": platform_goal,
        })
        timeline_pos += dur

    # 读取模板信息
    template_data = load_json("video_template.json") if False else None  # load_json 不在此作用域
    template_path = os.path.join(date_dir, "video_template.json")
    if os.path.exists(template_path):
        with open(template_path, encoding="utf-8") as f:
            template_data = json.load(f)
    else:
        template_data = None

    plan = {
        "version": "auto-plan-v3",
        "created_at": datetime.datetime.now().isoformat(),
        "title": f"{theme}记录",
        "video_template": template_data.get("video_template", "one_problem") if template_data else "one_problem",
        "template_name": template_data.get("template_name", "一个问题解决型") if template_data else "一个问题解决型",
        "raw_dir": raw_dir,
        "target_duration": 66,
        "actual_duration": timeline_pos,
        "render": OUTPUT_SPEC,
        "clips": plan_clips,
    }

    plan_path = os.path.join(date_dir, "edit_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    return {"plan": plan}


# ═══════════════════════════════════════════════════════════
# Stage 9: 发布包生成
# ═══════════════════════════════════════════════════════════

PUBLISH_PACK_PROMPT = """你是短视频发布专家。根据以下视频信息，生成发布包。

## 视频信息
- 标题: {title}
- 副标题: {subtitle}
- 故事线: {storyline}
- 模板: {template_name}
- 调性: {tone}
- 时长: {duration}秒
- 片段描述: {clips_desc}

## 生成要求
输出 JSON，包含：
- title_candidates: 3个标题候选（口语化、有悬念、15字以内）
- cover_text_candidates: 3个封面文字候选（6-8个字，干净有力）
- description: 一段发布简介（30字以内，自然不做作）
- hashtags: 4-6个话题标签
- comment_prompt: 一条引导评论的互动问题（15字以内）
- platform_notes: 抖音/小红书/视频号各自的发布注意事项

请直接输出 JSON，不要其他文字。"""


def stage_publish_pack(project_id: str, theme: str = "日常生活记录") -> dict:
    """生成发布包：标题、封面字、简介、话题、评论引导"""
    import openai

    date_dir = os.path.join(ROOT_DIR, project_id)
    update_progress(project_id, "publish_pack", 10, "准备发布包...")

    # 读取已有数据
    script = {}
    script_path = os.path.join(date_dir, "story_script.json")
    if os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            script = json.load(f)

    plan = {}
    plan_path = os.path.join(date_dir, "edit_plan.json")
    if os.path.exists(plan_path):
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)

    template_data = {}
    template_path = os.path.join(date_dir, "video_template.json")
    if os.path.exists(template_path):
        with open(template_path, encoding="utf-8") as f:
            template_data = json.load(f)

    # 构建片段描述
    clips_desc = ""
    for clip in plan.get("clips", [])[:8]:
        clips_desc += f"- [{clip.get('role', '?')}] {clip.get('caption', '?')} ({clip.get('start', 0):.1f}-{clip.get('end', 0):.1f}s)\n"

    # 尝试 AI 生成
    xiaomi_key = os.environ.get("XIAOMI_API_KEY", "")
    xiaomi_base = os.environ.get("XIAOMI_BASE_URL", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base = os.environ.get("OPENAI_BASE_URL", "")

    api_key = xiaomi_key or openai_key
    base_url = xiaomi_base or openai_base
    model = os.environ.get("STORY_MODEL", "mimo-v2.5-pro")

    pack = None
    if api_key:
        try:
            prompt = PUBLISH_PACK_PROMPT.format(
                title=script.get("title", f"{theme}记录"),
                subtitle=script.get("subtitle", ""),
                storyline=script.get("storyline", ""),
                template_name=template_data.get("template_name", ""),
                tone=script.get("tone", ""),
                duration=plan.get("actual_duration", 60),
                clips_desc=clips_desc or "(无片段信息)",
            )

            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = openai.OpenAI(**client_kwargs)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.8,
            )
            text = response.choices[0].message.content.strip()

            # 提取 JSON
            pack = extract_json_from_text(text)
        except Exception as e:
            print(f"  Publish pack API 错误: {e}")

    # Fallback: 从已有数据构建
    if not pack:
        title = script.get("title", f"{theme}记录")
        pack = {
            "title_candidates": [title, f"今天的{theme}", f"{theme}小记"],
            "cover_text_candidates": [title[:8], f"{theme}了", "整理完了"],
            "description": script.get("storyline", f"记录{theme}的过程"),
            "hashtags": [theme, "生活记录", "花园", "日常"],
            "comment_prompt": "你喜欢这样的生活吗？",
            "platform_notes": {
                "douyin": "标题口语化，前3秒放最吸引人的画面",
                "xiaohongshu": "封面字干净，简介补充花草名称",
                "shipinhao": "节奏可以略慢，保留生活感",
            },
        }

    # 保存
    pack["project_id"] = project_id
    pack["generated_at"] = datetime.datetime.now().isoformat()
    pack_path = os.path.join(date_dir, "publish_pack.json")
    with open(pack_path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)

    update_progress(project_id, "publish_pack", 100, "发布包生成完成")
    return pack


# ═══════════════════════════════════════════════════════════
# Stage 10: 发布后反馈记录
# ═══════════════════════════════════════════════════════════

def stage_record_performance(project_id: str, data: dict) -> dict:
    """记录发布后的平台数据，用于反馈学习"""
    date_dir = os.path.join(ROOT_DIR, project_id)

    # 读取已有数据
    perf_path = os.path.join(date_dir, "performance.json")
    performances = []
    if os.path.exists(perf_path):
        with open(perf_path, encoding="utf-8") as f:
            performances = json.load(f)
        if not isinstance(performances, list):
            performances = [performances]

    # 读取模板信息
    template_data = {}
    template_path = os.path.join(date_dir, "video_template.json")
    if os.path.exists(template_path):
        with open(template_path, encoding="utf-8") as f:
            template_data = json.load(f)

    # 构建记录
    perf_record = {
        "project_id": project_id,
        "platform": data.get("platform", "douyin"),
        "published_at": data.get("published_at", datetime.datetime.now().strftime("%Y-%m-%d")),
        "template": template_data.get("video_template", "unknown"),
        "template_name": template_data.get("template_name", ""),
        "title": data.get("title", ""),
        "duration": data.get("duration", 0),
        "views": data.get("views", 0),
        "likes": data.get("likes", 0),
        "comments": data.get("comments", 0),
        "saves": data.get("saves", 0),
        "shares": data.get("shares", 0),
        "avg_watch_time": data.get("avg_watch_time", 0),
        "completion_rate": data.get("completion_rate", 0),
        "recorded_at": datetime.datetime.now().isoformat(),
    }

    performances.append(perf_record)

    with open(perf_path, "w", encoding="utf-8") as f:
        json.dump(performances, f, ensure_ascii=False, indent=2)

    return {"status": "recorded", "record": perf_record, "total_records": len(performances)}


def stage_weekly_report(project_id: str = None) -> dict:
    """基于 performance.json 生成周报分析"""
    # 如果指定了项目，只分析该项目
    if project_id:
        date_dir = os.path.join(ROOT_DIR, project_id)
        perf_path = os.path.join(date_dir, "performance.json")
        if not os.path.exists(perf_path):
            return {"error": "无发布数据"}
        with open(perf_path, encoding="utf-8") as f:
            performances = json.load(f)
        if not isinstance(performances, list):
            performances = [performances]
    else:
        # 扫描所有项目的 performance.json
        performances = []
        for name in sorted(os.listdir(ROOT_DIR), reverse=True):
            date_dir = os.path.join(ROOT_DIR, name)
            if not os.path.isdir(date_dir) or name == "Inbox":
                continue
            # 支持两层目录
            subdirs = [date_dir]
            if not os.path.isdir(os.path.join(date_dir, "raw")):
                subdirs = [os.path.join(date_dir, d) for d in os.listdir(date_dir)
                          if os.path.isdir(os.path.join(date_dir, d))]
            for sd in subdirs:
                perf_path = os.path.join(sd, "performance.json")
                if os.path.exists(perf_path):
                    with open(perf_path, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        performances.extend(data)
                    else:
                        performances.append(data)

    if not performances:
        return {"error": "无发布数据", "performances": []}

    # 分析
    by_template = {}
    by_platform = {}
    for p in performances:
        tmpl = p.get("template", "unknown")
        plat = p.get("platform", "unknown")
        by_template.setdefault(plat, []).append(p)
        by_platform.setdefault(plat, []).append(p)

    # 按模板统计
    template_stats = {}
    for p in performances:
        tmpl = p.get("template", "unknown")
        if tmpl not in template_stats:
            template_stats[tmpl] = {"count": 0, "total_views": 0, "total_likes": 0, "total_comments": 0,
                                    "total_saves": 0, "total_shares": 0, "total_completion": 0}
        s = template_stats[tmpl]
        s["count"] += 1
        s["total_views"] += p.get("views", 0)
        s["total_likes"] += p.get("likes", 0)
        s["total_comments"] += p.get("comments", 0)
        s["total_saves"] += p.get("saves", 0)
        s["total_shares"] += p.get("shares", 0)
        s["total_completion"] += p.get("completion_rate", 0)

    # 计算平均值
    for tmpl, s in template_stats.items():
        n = s["count"]
        s["avg_views"] = round(s["total_views"] / n)
        s["avg_likes"] = round(s["total_likes"] / n)
        s["avg_comments"] = round(s["total_comments"] / n)
        s["avg_completion"] = round(s["total_completion"] / n, 2)

    # 按观看量排序模板
    ranked = sorted(template_stats.items(), key=lambda x: x[1]["avg_views"], reverse=True)

    report = {
        "total_videos": len(performances),
        "template_stats": template_stats,
        "ranking": [{"template": t, **s} for t, s in ranked],
        "insights": [],
    }

    # 生成洞察
    if len(ranked) >= 2:
        best = ranked[0]
        worst = ranked[-1]
        report["insights"].append(f"「{best[0]}」类视频平均观看最高 ({best[1]['avg_views']})，优于「{worst[0]}」({worst[1]['avg_views']})")

    # 时长分析
    durations = [p.get("duration", 0) for p in performances if p.get("duration", 0) > 0]
    if durations:
        avg_dur = sum(durations) / len(durations)
        # 找完播率最高的时长区间
        high_completion = [p for p in performances if p.get("completion_rate", 0) >= 0.3]
        if high_completion:
            hc_durations = [p.get("duration", 0) for p in high_completion if p.get("duration", 0) > 0]
            if hc_durations:
                avg_hc_dur = sum(hc_durations) / len(hc_durations)
                report["insights"].append(f"完播率≥30%的视频平均时长 {avg_hc_dur:.0f}秒")

    # 保存报告
    report_path = os.path.join(ROOT_DIR, "weekly_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


# ═══════════════════════════════════════════════════════════
# 全流程
# ═══════════════════════════════════════════════════════════

def run_full_pipeline(project_id: str, theme: str = "日常生活记录", target_duration: int = 60) -> dict:
    """执行完整流程"""
    results = {"stages": {}}

    try:
        # Stage 2: 关键帧
        update_progress(project_id, "pipeline", 10, "抽取关键帧...")
        results["stages"]["keyframes"] = stage_keyframes(project_id)

        # Stage 3: Contact sheets
        update_progress(project_id, "pipeline", 20, "生成 Contact Sheet...")
        results["stages"]["contact_sheets"] = stage_contact_sheets(project_id)

        # Stage 4: 视觉分析
        update_progress(project_id, "pipeline", 30, "视觉分析...")
        results["stages"]["visual_analysis"] = stage_visual_analysis(project_id, theme)

        # Stage 5: 转写
        update_progress(project_id, "pipeline", 50, "音频转字幕...")
        results["stages"]["transcription"] = stage_transcription(project_id)

        # Stage 5.5: 模板选择
        update_progress(project_id, "pipeline", 55, "选择视频模板...")
        results["stages"]["template_select"] = stage_select_template(project_id)

        # Stage 6: 故事脚本
        update_progress(project_id, "pipeline", 70, "生成剧情脚本...")
        results["stages"]["story_script"] = stage_story_script(project_id, theme, target_duration=target_duration)

        # Stage 7: 编辑计划
        update_progress(project_id, "pipeline", 80, "生成编辑计划...")
        results["stages"]["edit_plan"] = stage_edit_plan(project_id, theme)

        # Stage 8: 渲染
        update_progress(project_id, "pipeline", 85, "渲染视频...")
        results["stages"]["render"] = stage_render(project_id)

        # Stage 9: 发布包
        update_progress(project_id, "pipeline", 95, "生成发布包...")
        results["stages"]["publish_pack"] = stage_publish_pack(project_id, theme)

        update_progress(project_id, "pipeline", 100, "✅ 全流程完成!")
    except Exception as e:
        update_progress(project_id, "error", -1, f"错误: {e}")
        results["error"] = str(e)
        traceback.print_exc()

    return results


# ═══════════════════════════════════════════════════════════
# Flask App
# ═══════════════════════════════════════════════════════════

app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects", methods=["GET"])
def api_projects():
    return jsonify(list_projects())


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    """创建新项目"""
    data = request.json or {}
    date_str = data.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
    topic_input = data.get("topic", "")
    topic_slug = data.get("topic_slug", "")

    if not topic_input and not topic_slug:
        return jsonify({"error": "请提供主题名 (topic) 或主题 slug (topic_slug)"}), 400

    # 如果提供了中文主题名，自动生成 slug
    if topic_input and not topic_slug:
        topic_slug = slugify(topic_input)

    project_id = f"{date_str}/{topic_slug}"
    project_dir = ensure_dir(ROOT_DIR, date_str, topic_slug, "raw")

    # 创建主题元数据文件
    meta_path = os.path.join(ROOT_DIR, date_str, topic_slug, "meta.json")
    if not os.path.exists(meta_path):
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "topic": topic_input or topic_slug,
                "slug": topic_slug,
                "created_at": datetime.datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    return jsonify({
        "id": project_id,
        "date": date_str,
        "topic": topic_input or topic_slug,
        "topic_slug": topic_slug,
        "dir": project_dir,
    })


@app.route("/api/projects/<path:project_id>", methods=["GET"])
def api_project_detail(project_id):
    try:
        project_dir = resolve_project_dir(project_id)
    except ValueError as e:
        return error_response("INVALID_PROJECT_ID", str(e), status=400)
    if not os.path.isdir(project_dir):
        return error_response("PROJECT_NOT_FOUND", f"项目不存在: {project_id}", status=404)
    return jsonify(get_project_data(project_id))


@app.route("/api/projects/<path:project_id>/progress", methods=["GET"])
def api_progress(project_id):
    with progress_lock:
        data = progress_store.get(project_id, {"stage": "idle", "percent": 0, "message": ""})
    return jsonify(data)


@app.route("/api/projects/<path:project_id>/progress/stream")
def api_progress_stream(project_id):
    """SSE endpoint for real-time progress"""
    def generate():
        last_data = None
        while True:
            with progress_lock:
                data = progress_store.get(project_id, {"stage": "idle", "percent": 0, "message": ""})
            if data != last_data:
                yield f"data: {json.dumps(data)}\n\n"
                last_data = data
                if data.get("percent", 0) >= 100 or data.get("stage") == "error":
                    break
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")


# --- Pipeline stage endpoints ---

@app.route("/api/projects/<path:project_id>/import", methods=["POST"])
def api_import(project_id):
    topic = request.json.get("topic", "default") if request.json else "default"
    def run():
        try:
            result = stage_import(project_id, topic)
            return result
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/keyframes", methods=["POST"])
def api_keyframes(project_id):
    fps = float(request.json.get("fps_interval", 2.0)) if request.json else 2.0
    scene = float(request.json.get("scene_threshold", 0.35)) if request.json else 0.35
    def run():
        try:
            return stage_keyframes(project_id, fps, scene)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/contact-sheets", methods=["POST"])
def api_contact_sheets(project_id):
    def run():
        try:
            return stage_contact_sheets(project_id)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/visual-analysis", methods=["POST"])
def api_visual_analysis(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    def run():
        try:
            return stage_visual_analysis(project_id, theme)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/transcription", methods=["POST"])
def api_transcription(project_id):
    def run():
        try:
            return stage_transcription(project_id)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/template-select", methods=["POST"])
def api_template_select(project_id):
    def run():
        try:
            return stage_select_template(project_id)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/story-script", methods=["POST"])
def api_story_script(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    duration = request.json.get("duration", 60) if request.json else 60
    def run():
        try:
            return stage_story_script(project_id, theme, target_duration=duration)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/edit-plan", methods=["POST"])
def api_edit_plan(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    def run():
        try:
            return stage_edit_plan(project_id, theme)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/publish-pack", methods=["POST"])
def api_publish_pack(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    def run():
        try:
            return stage_publish_pack(project_id, theme)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<path:project_id>/performance", methods=["POST"])
def api_record_performance(project_id):
    data = request.json or {}
    try:
        return jsonify(stage_record_performance(project_id, data))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/report", methods=["GET"])
def api_weekly_report():
    try:
        return jsonify(stage_weekly_report())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<path:project_id>/render", methods=["POST"])
def api_render(project_id):
    # 渲染也需要项目锁，防止并发渲染
    if not acquire_project_lock(project_id):
        return error_response(
            "RENDER_RUNNING", f"项目 {project_id} 正在渲染中",
            stage="render", status=409
        )

    burn = request.json.get("burn_subtitles", True) if request.json else True
    audio_mode = request.json.get("audio_mode", "source") if request.json else "source"
    mode = request.json.get("mode", "publish") if request.json else "publish"
    show_debug = request.json.get("show_debug_overlay", False) if request.json else False

    def run():
        try:
            return stage_render(project_id, burn_subtitles=burn, audio_mode=audio_mode,
                               mode=mode, show_debug_overlay=show_debug)
        except Exception as e:
            return {"error": str(e)}
        finally:
            release_project_lock(project_id)
    return jsonify(run())


# --- 编辑保存 API ---

def _sync_script_to_plan(project_id: str, script: dict) -> dict:
    """从故事脚本同步生成编辑计划"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    raw_dir = os.path.join(date_dir, "raw")

    # 获取可用视频列表
    available_videos = {}
    if os.path.isdir(raw_dir):
        for fname in os.listdir(raw_dir):
            if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                fpath = os.path.join(raw_dir, fname)
                available_videos[fname] = get_video_info(fpath)

    clips = script.get("clips", [])
    enriched_clips = []
    timeline_pos = 0.0

    for clip in clips:
        src_name = clip.get("source", "")
        src_path = os.path.join(raw_dir, src_name) if src_name else ""

        # 模糊匹配
        if src_name and not os.path.exists(src_path):
            # 尝试前缀匹配
            prefix = src_name.split("_")[0] if "_" in src_name else src_name[:8]
            matches = glob.glob(os.path.join(raw_dir, f"*{prefix}*"))
            if matches:
                src_path = matches[0]
                src_name = os.path.basename(matches[0])

        # 如果还是找不到，用第一个可用视频
        if not os.path.exists(src_path) and available_videos:
            src_name = list(available_videos.keys())[0]
            src_path = os.path.join(raw_dir, src_name)

        if not os.path.exists(src_path):
            continue

        info = available_videos.get(src_name, get_video_info(src_path))
        start = float(clip.get("start", 0))
        end = float(clip.get("end", start + 10))
        dur = float(clip.get("duration", end - start))

        # 钳制到视频时长
        src_dur = info.get("duration", 999)
        if start >= src_dur:
            start = 0
        if end > src_dur:
            end = src_dur
        dur = end - start

        enriched_clips.append({
            "id": clip.get("id", f"{len(enriched_clips)+1:03d}"),
            "role": clip.get("role", "content"),
            "source": src_name,
            "source_path": src_path,
            "source_duration": src_dur,
            "source_has_audio": info.get("has_audio", False),
            "start": start,
            "end": end,
            "duration": dur,
            "timeline_start": timeline_pos,
            "timeline_end": timeline_pos + dur,
            "caption": clip.get("caption", ""),
            "voiceover_text": clip.get("voiceover_text", ""),
            "transition": clip.get("transition", ""),
            "note": clip.get("note", ""),
        })
        timeline_pos += dur

    plan = {
        "version": "user-edited-v1",
        "created_at": datetime.datetime.now().isoformat(),
        "title": script.get("title", ""),
        "subtitle": script.get("subtitle", ""),
        "storyline": script.get("storyline", ""),
        "tone": script.get("tone", ""),
        "hashtags": script.get("hashtags", []),
        "raw_dir": raw_dir,
        "target_duration": script.get("duration_target", 66),
        "actual_duration": timeline_pos,
        "render": OUTPUT_SPEC,
        "clips": enriched_clips,
    }

    plan_path = os.path.join(date_dir, "edit_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    return plan


@app.route("/api/projects/<path:project_id>/story-script", methods=["PUT"])
def api_save_story_script(project_id):
    """保存编辑后的故事脚本，自动同步到剪辑计划"""
    try:
        date_dir = resolve_project_dir(project_id)
    except ValueError as e:
        return error_response("INVALID_PROJECT_ID", str(e), status=400)
    if not os.path.isdir(date_dir):
        return error_response("PROJECT_NOT_FOUND", f"项目不存在: {project_id}", status=404)

    script = request.json
    if not script:
        return error_response("EMPTY_BODY", "请求体为空", status=400)

    # 保存故事脚本
    script_path = os.path.join(date_dir, "story_script.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    # 自动同步到编辑计划
    plan = _sync_script_to_plan(project_id, script)

    return jsonify({
        "status": "saved",
        "script_path": script_path,
        "plan_clips": len(plan.get("clips", [])),
        "plan_duration": plan.get("actual_duration", 0),
        "plan": plan,
    })


@app.route("/api/projects/<path:project_id>/edit-plan", methods=["PUT"])
def api_save_edit_plan(project_id):
    """保存编辑后的剪辑计划"""
    try:
        date_dir = resolve_project_dir(project_id)
    except ValueError as e:
        return error_response("INVALID_PROJECT_ID", str(e), status=400)
    if not os.path.isdir(date_dir):
        return error_response("PROJECT_NOT_FOUND", f"项目不存在: {project_id}", status=404)

    plan = request.json
    if not plan:
        return error_response("EMPTY_BODY", "请求体为空", status=400)

    # 确保有必要的字段
    plan.setdefault("version", "user-edited-v1")
    plan["created_at"] = datetime.datetime.now().isoformat()

    # 重新计算时间线
    timeline_pos = 0.0
    raw_dir = os.path.join(date_dir, "raw")
    for clip in plan.get("clips", []):
        dur = float(clip.get("duration", clip.get("end", 0) - clip.get("start", 0)))
        clip["timeline_start"] = timeline_pos
        clip["timeline_end"] = timeline_pos + dur
        # 补全 source_path
        src_name = clip.get("source", "")
        if src_name and not clip.get("source_path"):
            clip["source_path"] = os.path.join(raw_dir, src_name)
        timeline_pos += dur

    plan["actual_duration"] = timeline_pos

    plan_path = os.path.join(date_dir, "edit_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    return jsonify({
        "status": "saved",
        "plan_path": plan_path,
        "clips": len(plan.get("clips", [])),
        "duration": timeline_pos,
    })


@app.route("/api/projects/<path:project_id>/full-pipeline", methods=["POST"])
def api_full_pipeline(project_id):
    # 项目级并发锁
    if not acquire_project_lock(project_id):
        return error_response(
            "PIPELINE_RUNNING", f"项目 {project_id} 正在处理中",
            stage="pipeline", status=409
        )

    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    duration = request.json.get("duration", 60) if request.json else 60

    def run_bg():
        try:
            run_full_pipeline(project_id, theme, target_duration=duration)
        except Exception as e:
            update_progress(project_id, "error", -1, f"全流程失败: {e}")
        finally:
            release_project_lock(project_id)

    thread = threading.Thread(target=run_bg, daemon=True)
    thread.start()
    return jsonify({"status": "started", "project_id": project_id, "theme": theme})


# --- Static file serving ---

@app.route("/api/file/<path:filepath>")
def api_serve_file(filepath):
    """安全地提供文件 — 使用集中路径校验"""
    try:
        abs_path = os.path.realpath(os.path.abspath(filepath))
        root = os.path.realpath(ROOT_DIR)
        if not abs_path.startswith(root + os.sep):
            return error_response("ACCESS_DENIED", "路径越界", status=403)
        return send_from_directory(os.path.dirname(abs_path), os.path.basename(abs_path))
    except Exception as e:
        return error_response("FILE_ERROR", str(e), status=404)


@app.route("/api/video/<path:project_id>/<filename>")
def api_video(project_id, filename):
    """提供视频文件 — 集中路径校验"""
    try:
        file_path = resolve_file_path(project_id, "raw", filename)
        return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
    except ValueError as e:
        return error_response("PATH_VIOLATION", str(e), status=403)
    except Exception as e:
        return error_response("FILE_NOT_FOUND", str(e), status=404)


@app.route("/api/keyframe/<path:project_id>/<sub>/<filename>")
def api_keyframe(project_id, sub, filename):
    """提供关键帧图片 — 集中路径校验"""
    try:
        file_path = resolve_file_path(project_id, f"keyframes/{sub}", filename)
        return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
    except ValueError as e:
        return error_response("PATH_VIOLATION", str(e), status=403)
    except Exception as e:
        return error_response("FILE_NOT_FOUND", str(e), status=404)


@app.route("/api/contact-sheet/<path:project_id>/<filename>")
def api_contact_sheet(project_id, filename):
    """提供 contact sheet — 集中路径校验"""
    try:
        file_path = resolve_file_path(project_id, "contact_sheets", filename)
        return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
    except ValueError as e:
        return error_response("PATH_VIOLATION", str(e), status=403)
    except Exception as e:
        return error_response("FILE_NOT_FOUND", str(e), status=404)


@app.route("/api/output/<path:project_id>/<filename>")
def api_output(project_id, filename):
    """提供输出文件 — 集中路径校验"""
    try:
        file_path = resolve_file_path(project_id, "outputs", filename)
        return send_from_directory(os.path.dirname(file_path), os.path.basename(file_path))
    except ValueError as e:
        return error_response("PATH_VIOLATION", str(e), status=403)
    except Exception as e:
        return error_response("FILE_NOT_FOUND", str(e), status=404)


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

def main():
    global ROOT_DIR, INBOX_DIR

    parser = argparse.ArgumentParser(description="Garden AutoCut Web UI")
    parser.add_argument("--port", type=int, default=8766, help="端口号 (默认 8766)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--data-dir", default=ROOT_DIR, help="数据目录 (默认 ~/Movies/GardenAutoCut)")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    # 允许通过命令行覆盖数据目录
    if args.data_dir != ROOT_DIR:
        ROOT_DIR = os.path.expanduser(args.data_dir)
        INBOX_DIR = os.path.join(ROOT_DIR, "Inbox")

    print(f"🌿 Garden AutoCut Web UI")
    print(f"   http://{args.host}:{args.port}")
    print(f"   Root: {ROOT_DIR}")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
