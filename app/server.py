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


def list_projects() -> list[dict]:
    """列出所有项目（日期目录）"""
    projects = []
    if not os.path.isdir(ROOT_DIR):
        return projects
    for name in sorted(os.listdir(ROOT_DIR), reverse=True):
        date_dir = os.path.join(ROOT_DIR, name)
        raw_dir = os.path.join(date_dir, "raw")
        if os.path.isdir(date_dir) and os.path.isdir(raw_dir):
            videos = [f for f in os.listdir(raw_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
            outputs_dir = os.path.join(date_dir, "outputs")
            has_output = os.path.exists(os.path.join(outputs_dir, "rough_cut.mp4")) if os.path.isdir(outputs_dir) else False
            projects.append({
                "id": name,
                "date": name,
                "video_count": len(videos),
                "has_output": has_output,
                "has_analysis": os.path.exists(os.path.join(date_dir, "analysis.json")),
                "has_edit_plan": os.path.exists(os.path.join(date_dir, "edit_plan.json")),
                "has_story_script": os.path.exists(os.path.join(date_dir, "story_script.json")),
            })
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

    return {
        "id": project_id,
        "date": project_id,
        "dir": date_dir,
        "videos": videos,
        "keyframes": keyframes,
        "analysis": load_json("analysis.json"),
        "transcription": load_json("transcription.json"),
        "story_script": load_json("story_script.json"),
        "edit_plan": load_json("edit_plan.json"),
        "outputs": outputs,
    }


# ═══════════════════════════════════════════════════════════
# Stage 1: 视频导入
# ═══════════════════════════════════════════════════════════

def stage_import(project_id: str = None) -> dict:
    """扫描 Inbox，按日期归档"""
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
        raw_dir = ensure_dir(ROOT_DIR, date_str, "raw")
        dst = os.path.join(raw_dir, fname)

        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.move(src, dst)
            imported.append({"file": fname, "date": date_str})
            print(f"  归档: {fname} → {date_str}/raw/")

    update_progress(project_id or "import", "import", 100, f"导入完成: {len(imported)} 个视频")
    return {"imported": imported}


def stage_import_direct(files: list[str], project_id: str = None) -> dict:
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
        date_str = project_id or ct.strftime("%Y-%m-%d")
        raw_dir = ensure_dir(ROOT_DIR, date_str, "raw")
        dst = os.path.join(raw_dir, fname)

        if os.path.abspath(fpath) != os.path.abspath(dst):
            shutil.copy2(fpath, dst)
        imported.append({"file": fname, "date": date_str})

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

def stage_contact_sheets(project_id: str, max_frames: int = 30) -> dict:
    """把关键帧拼成 contact sheet"""
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
            frames = [frames[int(i * step)] for i in range(max_frames)]
        else:
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
                # 编号标签
                draw.text((x + 5, y + 5), f"#{i+1}", fill=(255, 200, 0))
            except Exception as e:
                print(f"  无法打开帧 {fp}: {e}")

        sheet_path = os.path.join(cs_dir, f"{sub}_sheet.jpg")
        sheet.save(sheet_path, quality=85)
        sheets.append({"type": sub, "path": sheet_path, "frame_count": n})
        print(f"  Contact sheet: {sub}_sheet.jpg ({n} 帧)")

    return {"sheets": sheets}


# ═══════════════════════════════════════════════════════════
# Stage 4: 视觉分析
# ═══════════════════════════════════════════════════════════

VISION_PROMPT_TEMPLATE = """你是专业的视频内容分析师。请分析这些关键帧截图，为视频片段输出结构化 JSON。

用户主题偏好：{theme}

这些截图来自以下视频文件：
{video_list}

每张截图的编号对应关系：编号 1-N 的帧来自第一个视频，以此类推。请根据帧内容推断每段视频的最佳使用方式。

对每个视频文件，输出一个 JSON 对象，包含：
- file: **必须使用上面列出的实际视频文件名**（如 5894_raw.MP4），不要用 frame_set 或其他名称
- start: 建议片段起始秒数（基于视频总时长和画面内容）
- end: 建议片段结束秒数
- duration: 建议片段时长(秒)
- action: 画面中正在发生的动作（中文，15字以内）
- subjects: 画面中的主要元素列表
- story_role: 故事角色，从以下选一个：
  "opening"(空镜开场), "space"(交代环境), "action_intro"(动作引入),
  "action"(核心动作), "collect"(收集整理), "life"(生活气息),
  "result"(成果展示), "detail"(细节特写), "ending"(空镜收尾)
- quality_score: 画面质量 1-10
- stability: 画面稳定度 1-10
- privacy_risk: 隐私风险 1-10（1=无风险）
- caption: 一句适合做字幕的中文描述（15字以内，有文采但不做作）

请直接输出 JSON 数组，不要其他文字。"""


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
            max_tokens=4096,
            temperature=0.2,
        )
        text = response.choices[0].message.content.strip()

        # 提取 JSON
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                analysis = [json.loads(json_match.group())]
            else:
                analysis = []
    except Exception as e:
        print(f"  Vision API 错误: {e}")
        update_progress(project_id, "visual_analysis", 80, f"API 调用失败: {e}")
        # Fallback: 生成基础分析
        analysis = _fallback_visual_analysis(project_id)

    # 保存结果
    analysis_path = os.path.join(date_dir, "analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    update_progress(project_id, "visual_analysis", 100, f"视觉分析完成: {len(analysis)} 个片段")
    return {"analysis": analysis, "count": len(analysis)}


def _fallback_visual_analysis(project_id: str) -> list[dict]:
    """无 API 时的 fallback 分析"""
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
        results.append({
            "file": fname,
            "start": 0.0,
            "end": min(dur, 15.0),
            "duration": min(dur, 15.0),
            "action": "待分析",
            "subjects": [],
            "story_role": "action",
            "quality_score": 6,
            "stability": 7,
            "privacy_risk": 1,
            "caption": fname.split("_")[0],
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

    # 读取分析结果
    analysis_path = os.path.join(date_dir, "analysis.json")
    analysis = []
    if os.path.exists(analysis_path):
        with open(analysis_path, encoding="utf-8") as f:
            analysis = json.load(f)

    # 读取转写结果
    transcription_path = os.path.join(date_dir, "transcription.json")
    transcriptions = []
    if os.path.exists(transcription_path):
        with open(transcription_path, encoding="utf-8") as f:
            transcriptions = json.load(f)

    # 构建素材描述
    materials = ""
    for seg in analysis:
        materials += f"- {seg.get('file', '?')}: {seg.get('action', '?')} | 画面元素: {', '.join(seg.get('subjects', []))} | 质量:{seg.get('quality_score', 5)}/10 | 隐私风险:{seg.get('privacy_risk', 1)}/10\n"

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

def stage_render(project_id: str, burn_subtitles: bool = True, audio_mode: str = "source") -> dict:
    """渲染最终视频"""
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

    update_progress(project_id, "render", 5, f"准备渲染 {len(clips)} 个片段...")

    spec = OUTPUT_SPEC
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

    # 合并片段
    update_progress(project_id, "render", 85, "合并片段...")
    concat_file = os.path.join(out_dir, "_concat.txt")
    with open(concat_file, "w") as f:
        for clip_path in temp_clips:
            escaped = str(os.path.abspath(clip_path)).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    rough_cut = os.path.join(out_dir, "rough_cut.mp4")
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

    # 没有故事脚本，从分析结果生成
    analysis_path = os.path.join(date_dir, "analysis.json")
    if not os.path.exists(analysis_path):
        return {"error": "没有故事脚本或分析结果"}

    with open(analysis_path, encoding="utf-8") as f:
        analysis = json.load(f)

    # 评分排序
    for seg in analysis:
        seg["_score"] = (
            seg.get("quality_score", 5) * 0.30 +
            min(10, len(seg.get("action", "")) / 2) * 0.25 +
            (8 if seg.get("story_role") in [s["role"] for s in STORY_SLOTS] else 5) * 0.20 +
            seg.get("stability", 7) * 0.10 +
            (8 if any(k in seg.get("action", "") for k in {"花", "草", "树", "剪", "种", "浇"}) else 4) * 0.10
        )
        if seg.get("privacy_risk", 1) >= 5:
            seg["_score"] -= (seg["privacy_risk"] - 4) * 2

    # 按角色选最佳
    by_role = {}
    for seg in analysis:
        role = seg.get("story_role", "action")
        by_role.setdefault(role, []).append(seg)
    for role in by_role:
        by_role[role].sort(key=lambda s: s["_score"], reverse=True)

    selected = []
    for slot in STORY_SLOTS:
        candidates = by_role.get(slot["role"], [])
        if candidates:
            chosen = candidates[0]
            if not any(s.get("file") == chosen.get("file") for s in selected):
                selected.append(chosen)

    # 填充不足
    if len(selected) < 4:
        for seg in sorted(analysis, key=lambda s: s["_score"], reverse=True):
            if len(selected) >= len(STORY_SLOTS):
                break
            if not any(s.get("file") == seg.get("file") for s in selected):
                selected.append(seg)

    raw_dir = os.path.join(date_dir, "raw")
    plan_clips = []
    timeline_pos = 0.0
    for seg in selected:
        src_name = seg.get("file", "")
        src_path = os.path.join(raw_dir, src_name)
        if not os.path.exists(src_path):
            continue
        info = get_video_info(src_path)
        dur = float(seg.get("duration", seg.get("end", 10) - seg.get("start", 0)))

        plan_clips.append({
            "role": seg.get("story_role", "action"),
            "source": src_name,
            "source_path": src_path,
            "source_duration": info.get("duration", 0),
            "source_has_audio": info.get("has_audio", False),
            "start": float(seg.get("start", 0)),
            "end": float(seg.get("end", dur)),
            "duration": dur,
            "timeline_start": timeline_pos,
            "timeline_end": timeline_pos + dur,
            "caption": seg.get("caption", ""),
            "note": seg.get("action", ""),
        })
        timeline_pos += dur

    plan = {
        "version": "auto-plan-v1",
        "created_at": datetime.datetime.now().isoformat(),
        "title": f"{theme}记录",
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

        # Stage 6: 故事脚本
        update_progress(project_id, "pipeline", 70, "生成剧情脚本...")
        results["stages"]["story_script"] = stage_story_script(project_id, theme, target_duration=target_duration)

        # Stage 7: 编辑计划
        update_progress(project_id, "pipeline", 80, "生成编辑计划...")
        results["stages"]["edit_plan"] = stage_edit_plan(project_id, theme)

        # Stage 8: 渲染
        update_progress(project_id, "pipeline", 85, "渲染视频...")
        results["stages"]["render"] = stage_render(project_id)

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


@app.route("/api/projects/<project_id>", methods=["GET"])
def api_project_detail(project_id):
    return jsonify(get_project_data(project_id))


@app.route("/api/projects/<project_id>/progress", methods=["GET"])
def api_progress(project_id):
    with progress_lock:
        data = progress_store.get(project_id, {"stage": "idle", "percent": 0, "message": ""})
    return jsonify(data)


@app.route("/api/projects/<project_id>/progress/stream")
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

@app.route("/api/projects/<project_id>/import", methods=["POST"])
def api_import(project_id):
    def run():
        try:
            result = stage_import(project_id)
            return result
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/keyframes", methods=["POST"])
def api_keyframes(project_id):
    fps = float(request.json.get("fps_interval", 2.0)) if request.json else 2.0
    scene = float(request.json.get("scene_threshold", 0.35)) if request.json else 0.35
    def run():
        try:
            return stage_keyframes(project_id, fps, scene)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/contact-sheets", methods=["POST"])
def api_contact_sheets(project_id):
    def run():
        try:
            return stage_contact_sheets(project_id)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/visual-analysis", methods=["POST"])
def api_visual_analysis(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    def run():
        try:
            return stage_visual_analysis(project_id, theme)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/transcription", methods=["POST"])
def api_transcription(project_id):
    def run():
        try:
            return stage_transcription(project_id)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/story-script", methods=["POST"])
def api_story_script(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    duration = request.json.get("duration", 60) if request.json else 60
    def run():
        try:
            return stage_story_script(project_id, theme, target_duration=duration)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/edit-plan", methods=["POST"])
def api_edit_plan(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    def run():
        try:
            return stage_edit_plan(project_id, theme)
        except Exception as e:
            return {"error": str(e)}
    return jsonify(run())


@app.route("/api/projects/<project_id>/render", methods=["POST"])
def api_render(project_id):
    burn = request.json.get("burn_subtitles", True) if request.json else True
    audio_mode = request.json.get("audio_mode", "source") if request.json else "source"
    def run():
        try:
            return stage_render(project_id, burn_subtitles=burn, audio_mode=audio_mode)
        except Exception as e:
            return {"error": str(e)}
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


@app.route("/api/projects/<project_id>/story-script", methods=["PUT"])
def api_save_story_script(project_id):
    """保存编辑后的故事脚本，自动同步到剪辑计划"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    if not os.path.isdir(date_dir):
        return jsonify({"error": "项目不存在"}), 404

    script = request.json
    if not script:
        return jsonify({"error": "请求体为空"}), 400

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


@app.route("/api/projects/<project_id>/edit-plan", methods=["PUT"])
def api_save_edit_plan(project_id):
    """保存编辑后的剪辑计划"""
    date_dir = os.path.join(ROOT_DIR, project_id)
    if not os.path.isdir(date_dir):
        return jsonify({"error": "项目不存在"}), 404

    plan = request.json
    if not plan:
        return jsonify({"error": "请求体为空"}), 400

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


@app.route("/api/projects/<project_id>/full-pipeline", methods=["POST"])
def api_full_pipeline(project_id):
    theme = request.json.get("theme", "日常生活记录") if request.json else "日常生活记录"
    duration = request.json.get("duration", 60) if request.json else 60
    # 在后台线程中运行，通过 SSE 报告进度
    def run_bg():
        try:
            run_full_pipeline(project_id, theme, target_duration=duration)
        except Exception as e:
            update_progress(project_id, "error", -1, f"全流程失败: {e}")
    thread = threading.Thread(target=run_bg, daemon=True)
    thread.start()
    return jsonify({"status": "started", "project_id": project_id, "theme": theme})


# --- Static file serving ---

@app.route("/api/file/<path:filepath>")
def api_serve_file(filepath):
    """安全地提供文件"""
    abs_path = os.path.abspath(filepath)
    # 安全检查：只允许在 ROOT_DIR 下
    if not abs_path.startswith(os.path.abspath(ROOT_DIR)):
        return jsonify({"error": "Access denied"}), 403
    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    return send_from_directory(directory, filename)


@app.route("/api/video/<project_id>/<filename>")
def api_video(project_id, filename):
    """提供视频文件"""
    raw_dir = os.path.join(ROOT_DIR, project_id, "raw")
    return send_from_directory(raw_dir, filename)


@app.route("/api/keyframe/<project_id>/<sub>/<filename>")
def api_keyframe(project_id, sub, filename):
    """提供关键帧图片"""
    kf_dir = os.path.join(ROOT_DIR, project_id, "keyframes", sub)
    return send_from_directory(kf_dir, filename)


@app.route("/api/contact-sheet/<project_id>/<filename>")
def api_contact_sheet(project_id, filename):
    """提供 contact sheet"""
    cs_dir = os.path.join(ROOT_DIR, project_id, "contact_sheets")
    return send_from_directory(cs_dir, filename)


@app.route("/api/output/<project_id>/<filename>")
def api_output(project_id, filename):
    """提供输出文件"""
    out_dir = os.path.join(ROOT_DIR, project_id, "outputs")
    return send_from_directory(out_dir, filename)


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
