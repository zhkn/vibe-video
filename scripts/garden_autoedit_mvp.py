#!/usr/bin/env python3
"""
Garden AutoCut MVP — iPhone → Mac 自动剪辑工作流
用法:
  python3 garden_autoedit_mvp.py \
    --inbox ~/Movies/GardenAutoCut/Inbox \
    --root  ~/Movies/GardenAutoCut

流程:
  1. Inbox 视频按日期归档
  2. 固定 + 场景变化抽关键帧
  3. 生成 contact sheet，交给多模态模型解析
  4. 评分排序 → edit_plan.json
  5. ffmpeg 自动剪辑 → rough_cut.mp4 + captions.srt
"""

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
from typing import Optional

# ─── 常量 ───────────────────────────────────────────────
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".hevc", ".3gp"}
STORY_SLOTS = [
    "opening",   # 空镜开场
    "space",     # 交代花园
    "action",    # 修剪动作
    "collect",   # 红桶收集
    "life",      # 背影劳动
    "result",    # 成果展示
    "ending",    # 空镜收尾
]
OUTPUT_SPEC = {
    "aspect": "9:16",
    "width": 1080,
    "height": 1920,
    "codec": "libx264",
    "fps": 30,
    "min_duration": 55,
    "max_duration": 75,
}


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def run(cmd: list[str], check=True, capture=True, **kw) -> subprocess.CompletedProcess:
    """封装 subprocess.run，统一日志"""
    print(f"  $ {' '.join(cmd[:6])}{'…' if len(cmd)>6 else ''}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kw)


def get_creation_time(path: str) -> datetime.datetime:
    """优先用 ffprobe 读 creation_time，fallback 到文件 mtime"""
    try:
        r = run([
            "ffprobe", "-v", "quiet",
            "-show_entries", "format_tags=creation_time",
            "-of", "csv=p=0", path
        ], check=False)
        ts = r.stdout.strip()
        if ts:
            # ffprobe 返回 ISO 格式，带时区
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        pass
    # fallback: 文件 mtime
    mtime = os.path.getmtime(path)
    return datetime.datetime.fromtimestamp(mtime)


def get_duration(path: str) -> float:
    """用 ffprobe 获取视频时长（秒）"""
    r = run([
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", path
    ], check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def ensure_dir(*parts) -> str:
    p = os.path.join(*parts)
    os.makedirs(p, exist_ok=True)
    return p


def image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# ═══════════════════════════════════════════════════════════
# 第一步：按日期归档
# ═══════════════════════════════════════════════════════════

def archive_inbox(inbox: str, root: str, date_override: Optional[str] = None) -> dict[str, list[str]]:
    """
    扫描 inbox，按 creation_time 日期归档到 root/<date>/raw/
    返回 {日期字符串: [归档后文件路径列表]}
    """
    archive_map: dict[str, list[str]] = {}
    inbox = os.path.expanduser(inbox)
    root = os.path.expanduser(root)

    for fname in sorted(os.listdir(inbox)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTS:
            continue
        src = os.path.join(inbox, fname)
        if not os.path.isfile(src):
            continue

        ct = get_creation_time(src)
        date_str = date_override or ct.strftime("%Y-%m-%d")
        raw_dir = ensure_dir(root, date_str, "raw")
        dst = os.path.join(raw_dir, fname)

        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.move(src, dst)
            print(f"  归档: {fname} → {date_str}/raw/")
        else:
            dst = src  # 已经在正确位置

        archive_map.setdefault(date_str, []).append(dst)

    if not archive_map:
        print("  Inbox 为空，没有新视频。")
    return archive_map


# ═══════════════════════════════════════════════════════════
# 第二步：抽关键帧
# ═══════════════════════════════════════════════════════════

def extract_keyframes(video_path: str, date_dir: str, fps_interval: float = 2.0, scene_thresh: float = 0.35):
    """
    固定抽帧 + 场景变化抽帧
    返回 keyframes 目录路径
    """
    kf_dir = os.path.join(date_dir, "keyframes")
    fixed_dir = ensure_dir(kf_dir, "fixed")
    scene_dir = ensure_dir(kf_dir, "scene")

    base = os.path.splitext(os.path.basename(video_path))[0]

    # 固定抽帧：每 fps_interval 秒一帧
    run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps=1/{fps_interval}",
        os.path.join(fixed_dir, f"{base}_%04d.jpg")
    ], check=False)

    # 场景变化抽帧
    run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"select='gt(scene,{scene_thresh})'",
        "-vsync", "vfr",
        os.path.join(scene_dir, f"{base}_%04d.jpg")
    ], check=False)

    fixed_count = len(glob.glob(os.path.join(fixed_dir, f"{base}_*.jpg")))
    scene_count = len(glob.glob(os.path.join(scene_dir, f"{base}_*.jpg")))
    print(f"  关键帧: {base} → 固定 {fixed_count} 帧, 场景 {scene_count} 帧")
    return kf_dir


# ═══════════════════════════════════════════════════════════
# 第三步：生成 Contact Sheet
# ═══════════════════════════════════════════════════════════

def make_contact_sheet(kf_dir: str, date_dir: str, max_frames: int = 20) -> list[str]:
    """
    把关键帧拼成 contact sheet，返回所有 sheet 路径
    """
    from PIL import Image

    cs_dir = ensure_dir(date_dir, "contact_sheets")
    sheets = []

    for sub in ["fixed", "scene"]:
        sub_dir = os.path.join(kf_dir, sub)
        if not os.path.isdir(sub_dir):
            continue
        frames = sorted(glob.glob(os.path.join(sub_dir, "*.jpg")))
        if not frames:
            continue

        # 取前 max_frames 帧
        frames = frames[:max_frames]
        n = len(frames)
        cols = min(5, n)
        rows = math.ceil(n / cols)

        thumb_w, thumb_h = 324, 576  # 9:16 缩略图
        sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (30, 30, 30))
        for i, fp in enumerate(frames):
            img = Image.open(fp)
            img.thumbnail((thumb_w, thumb_h))
            x = (i % cols) * thumb_w
            y = (i // cols) * thumb_h
            # 居中放置
            ox = x + (thumb_w - img.width) // 2
            oy = y + (thumb_h - img.height) // 2
            sheet.paste(img, (ox, oy))

        sheet_path = os.path.join(cs_dir, f"{sub}_sheet.jpg")
        sheet.save(sheet_path, quality=85)
        sheets.append(sheet_path)
        print(f"  Contact sheet: {sub}_sheet.jpg ({n} 帧)")

    return sheets


# ═══════════════════════════════════════════════════════════
# 第四步：多模态模型解析
# ═══════════════════════════════════════════════════════════

VISION_PROMPT = """你是专业的花园劳动视频分析师。请分析这些关键帧截图，为视频片段输出结构化 JSON。

对每个片段（每个视频文件），输出一个 JSON 对象，包含：
- file: 文件名
- start: 建议片段起始秒数（基于画面内容）
- end: 建议片段结束秒数
- action: 画面中正在发生的动作（中文）
- story_role: 故事角色，从以下选一个：
  "opening"(空镜开场), "space"(交代花园), "action"(修剪动作),
  "collect"(收集), "life"(生活/背影), "result"(成果展示), "ending"(空镜收尾)
- quality_score: 画面质量 1-10（清晰度、构图、光线）
- privacy_risk: 隐私风险 1-10（1=无风险，10=明显正脸/隐私信息）
- caption: 一句适合做字幕的中文描述（15字以内）

请直接输出 JSON 数组，不要其他文字。如果有多张 contact sheet，综合分析。

花园劳动主题：修剪、浇水、除草、种植、收获等。"""


def analyze_with_vision(sheets: list[str], date_dir: str, model: str = "gpt-4o") -> list[dict]:
    """调用 OpenAI Vision API 分析 contact sheets"""
    import openai

    client = openai.OpenAI()

    content = [{"type": "text", "text": VISION_PROMPT}]
    for sp in sheets:
        b64 = image_to_base64(sp)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
        })

    print(f"  调用 {model} 分析画面...")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=4096,
        temperature=0.2,
    )

    text = response.choices[0].message.content.strip()
    # 提取 JSON（可能被 markdown 包裹）
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if json_match:
        data = json.loads(json_match.group())
    else:
        # 尝试单个对象
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = [json.loads(json_match.group())]
        else:
            print(f"  ⚠ 无法解析模型输出，使用默认分析")
            data = []

    return data


def generate_fallback_analysis(video_files: list[str]) -> list[dict]:
    """不调用 API 时的 fallback 分析"""
    results = []
    for vf in video_files:
        fname = os.path.basename(vf)
        dur = get_duration(vf)
        results.append({
            "file": fname,
            "start": 0.0,
            "end": min(dur, 15.0),
            "action": "花园劳动",
            "story_role": "action",
            "quality_score": 6,
            "privacy_risk": 1,
            "caption": "花园劳动中",
        })
    return results


# ═══════════════════════════════════════════════════════════
# 第五步：评分 + 生成剪辑计划
# ═══════════════════════════════════════════════════════════

def score_segment(seg: dict) -> float:
    """
    评分规则:
    画面质量 * 0.30 + 动作明确度 * 0.25 + 故事匹配度 * 0.20
    + 稳定度 * 0.10 + 花园识别度 * 0.10
    - 重复惩罚 - 正脸/隐私惩罚 - 楼体背景过重惩罚
    """
    quality = seg.get("quality_score", 5)
    # 动作明确度：caption 越具体越高
    action_clarity = min(10, len(seg.get("action", "")) / 2)
    # 故事匹配度：story_role 越明确越高
    role = seg.get("story_role", "action")
    story_match = 8 if role in STORY_SLOTS else 5
    # 稳定度：默认给 7（没有运动分析时）
    stability = 7
    # 花园识别度：action 包含花园关键词
    garden_keywords = {"花园", "修剪", "浇水", "除草", "种植", "收获", "花", "草", "树", "剪"}
    garden_recog = 8 if any(k in seg.get("action", "") for k in garden_keywords) else 4

    score = (
        quality * 0.30
        + action_clarity * 0.25
        + story_match * 0.20
        + stability * 0.10
        + garden_recog * 0.10
    )

    # 惩罚
    privacy = seg.get("privacy_risk", 1)
    if privacy >= 5:
        score -= (privacy - 4) * 2  # 隐私风险越高惩罚越大

    return round(score, 2)


def build_edit_plan(analysis: list[dict], date_dir: str) -> dict:
    """根据分析结果生成剪辑计划"""
    # 计算每个片段的分数
    for seg in analysis:
        seg["score"] = score_segment(seg)

    # 按 story_role 分组，每组内按分数排序
    by_role: dict[str, list[dict]] = {}
    for seg in analysis:
        role = seg.get("story_role", "action")
        by_role.setdefault(role, []).append(seg)
    for role in by_role:
        by_role[role].sort(key=lambda s: s["score"], reverse=True)

    # 为每个 story slot 选最佳片段
    selected: list[dict] = []
    for slot in STORY_SLOTS:
        candidates = by_role.get(slot, [])
        if candidates:
            # 取分数最高的，避免重复文件
            chosen = candidates[0]
            if not any(s["file"] == chosen["file"] for s in selected):
                selected.append(chosen)

    # 如果某些 slot 没有匹配，从 action 中补
    if len(selected) < 4:
        action_pool = by_role.get("action", [])
        for seg in action_pool:
            if len(selected) >= len(STORY_SLOTS):
                break
            if not any(s["file"] == seg["file"] for s in selected):
                selected.append(seg)

    plan = {
        "created": datetime.datetime.now().isoformat(),
        "target": OUTPUT_SPEC,
        "story_structure": STORY_SLOTS,
        "segments": selected,
        "total_score": sum(s["score"] for s in selected),
    }

    plan_path = os.path.join(date_dir, "edit_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"  剪辑计划: edit_plan.json ({len(selected)} 个片段)")
    return plan


# ═══════════════════════════════════════════════════════════
# 第六步：自动剪辑 + 字幕
# ═══════════════════════════════════════════════════════════

def auto_cut(date_dir: str, raw_dir: str, plan: dict):
    """根据 edit_plan 自动剪辑视频"""
    out_dir = ensure_dir(date_dir, "outputs")
    segments = plan.get("segments", [])
    if not segments:
        print("  ⚠ 没有可剪辑的片段")
        return

    target = plan["target"]
    total_min = target["min_duration"]
    total_max = target["max_duration"]

    # 计算每个片段的时长分配
    n = len(segments)
    avg_dur = (total_min + total_max) / 2 / max(n, 1)

    # 构建 ffmpeg concat 文件
    concat_list = []
    srt_entries = []
    current_time = 0.0

    for i, seg in enumerate(segments):
        src_file = os.path.join(raw_dir, seg["file"])
        if not os.path.exists(src_file):
            # 尝试模糊匹配
            matches = glob.glob(os.path.join(raw_dir, f"*{seg['file'][:8]}*"))
            if matches:
                src_file = matches[0]
            else:
                print(f"  ⚠ 找不到源文件: {seg['file']}")
                continue

        start = seg.get("start", 0)
        end = seg.get("end", start + avg_dur)
        clip_dur = min(end - start, avg_dur * 1.5)

        # 临时裁剪文件
        tmp_clip = os.path.join(out_dir, f"_tmp_clip_{i:03d}.mp4")
        run([
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", src_file,
            "-t", str(clip_dur),
            "-c:v", target["codec"],
            "-vf", f"scale={target['width']}:{target['height']}:force_original_aspect_ratio=decrease,pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2",
            "-r", str(target["fps"]),
            "-an",  # 暂时不带音频
            "-preset", "fast",
            tmp_clip
        ], check=False)

        if os.path.exists(tmp_clip) and os.path.getsize(tmp_clip) > 0:
            concat_list.append(tmp_clip)

            # 字幕条目
            caption = seg.get("caption", seg.get("action", ""))
            srt_entries.append({
                "index": i + 1,
                "start": current_time,
                "end": current_time + clip_dur,
                "text": caption,
            })
            current_time += clip_dur

    if not concat_list:
        print("  ⚠ 没有成功裁剪的片段")
        return

    # 合并所有片段
    concat_file = os.path.join(out_dir, "_concat.txt")
    with open(concat_file, "w") as f:
        for clip in concat_list:
            f.write(f"file '{clip}'\n")

    final_output = os.path.join(out_dir, "rough_cut.mp4")
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c:v", target["codec"],
        "-preset", "medium",
        "-crf", "23",
        final_output
    ], check=False)

    # 清理临时文件
    for clip in concat_list:
        os.remove(clip)
    os.remove(concat_file)

    if os.path.exists(final_output):
        dur = get_duration(final_output)
        size_mb = os.path.getsize(final_output) / 1024 / 1024
        print(f"  ✅ 输出: rough_cut.mp4 ({dur:.1f}s, {size_mb:.1f}MB)")
    else:
        print("  ⚠ 合成失败")

    # 生成 SRT 字幕
    srt_path = os.path.join(out_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for entry in srt_entries:
            f.write(f"{entry['index']}\n")
            f.write(f"{format_srt_time(entry['start'])} --> {format_srt_time(entry['end'])}\n")
            f.write(f"{entry['text']}\n\n")
    print(f"  ✅ 字幕: captions.srt ({len(srt_entries)} 条)")


def format_srt_time(seconds: float) -> str:
    """秒数转 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def process_date(date_str: str, video_files: list[str], root: str, args):
    """处理一天的素材"""
    print(f"\n{'='*60}")
    print(f"📅 处理日期: {date_str} ({len(video_files)} 个视频)")
    print(f"{'='*60}")

    date_dir = os.path.join(root, date_str)
    raw_dir = os.path.join(date_dir, "raw")

    # Step 2: 抽关键帧
    print("\n🎞️  Step 2: 抽关键帧")
    all_kf_dirs = []
    for vf in video_files:
        kf_dir = extract_keyframes(vf, date_dir)
        all_kf_dirs.append(kf_dir)

    # Step 3: 生成 Contact Sheet
    print("\n🖼️  Step 3: 生成 Contact Sheet")
    all_sheets = []
    for kf_dir in set(all_kf_dirs):
        sheets = make_contact_sheet(kf_dir, date_dir)
        all_sheets.extend(sheets)

    # Step 4: 多模态分析
    print("\n🔍 Step 4: 多模态画面分析")
    if args.skip_vision:
        print("  跳过 Vision API，使用 fallback 分析")
        analysis = generate_fallback_analysis(video_files)
    else:
        try:
            analysis = analyze_with_vision(all_sheets, date_dir, model=args.model)
            if not analysis:
                analysis = generate_fallback_analysis(video_files)
        except Exception as e:
            print(f"  ⚠ Vision API 调用失败: {e}")
            print("  使用 fallback 分析")
            analysis = generate_fallback_analysis(video_files)

    # 补充 file 字段（确保每个片段都有文件名）
    raw_files = [os.path.basename(f) for f in video_files]
    for seg in analysis:
        if "file" not in seg and raw_files:
            seg["file"] = raw_files[0]

    analysis_path = os.path.join(date_dir, "analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    print(f"  分析结果: analysis.json ({len(analysis)} 个片段)")

    # Step 5: 生成剪辑计划
    print("\n📋 Step 5: 生成剪辑计划")
    plan = build_edit_plan(analysis, date_dir)

    # Step 6: 自动剪辑
    if not args.skip_cut:
        print("\n✂️  Step 6: 自动剪辑")
        auto_cut(date_dir, raw_dir, plan)
    else:
        print("\n⏭️  跳过自动剪辑（--skip-cut）")


def main():
    parser = argparse.ArgumentParser(
        description="Garden AutoCut MVP — iPhone → Mac 自动剪辑工作流",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 完整流程（抽帧 + 分析 + 剪辑）
  python3 garden_autoedit_mvp.py --inbox ~/Movies/GardenAutoCut/Inbox --root ~/Movies/GardenAutoCut

  # 只生成计划，不剪辑
  python3 garden_autoedit_mvp.py --inbox ~/Movies/GardenAutoCut/Inbox --root ~/Movies/GardenAutoCut --skip-cut

  # 跳过 Vision API（不调用 OpenAI）
  python3 garden_autoedit_mvp.py --inbox ~/Movies/GardenAutoCut/Inbox --root ~/Movies/GardenAutoCut --skip-vision

  # 指定日期处理已有素材
  python3 garden_autoedit_mvp.py --root ~/Movies/GardenAutoCut --date 2026-06-04
""")
    parser.add_argument("--inbox", default="~/Movies/GardenAutoCut/Inbox", help="Inbox 目录路径")
    parser.add_argument("--root", default="~/Movies/GardenAutoCut", help="工作流根目录")
    parser.add_argument("--date", help="指定日期（YYYY-MM-DD），默认自动按 creation_time 归档")
    parser.add_argument("--model", default="gpt-4o", help="Vision 模型（默认 gpt-4o）")
    parser.add_argument("--skip-vision", action="store_true", help="跳过 Vision API，使用 fallback 分析")
    parser.add_argument("--skip-cut", action="store_true", help="只生成计划，不执行剪辑")
    args = parser.parse_args()

    root = os.path.expanduser(args.root)
    inbox = os.path.expanduser(args.inbox)

    print("🌿 Garden AutoCut MVP")
    print(f"   Inbox: {inbox}")
    print(f"   Root:  {root}")

    # Step 1: 归档
    print("\n📥 Step 1: 归档 Inbox 素材")
    archive_map = archive_inbox(inbox, root, date_override=args.date)

    # 如果指定了日期但 Inbox 为空，扫描已有 raw 目录
    if args.date and not archive_map:
        raw_dir = os.path.join(root, args.date, "raw")
        if os.path.isdir(raw_dir):
            files = []
            for f in os.listdir(raw_dir):
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    files.append(os.path.join(raw_dir, f))
            if files:
                archive_map[args.date] = files
                print(f"  找到已有素材: {len(files)} 个视频")

    # 处理每一天
    for date_str, video_files in sorted(archive_map.items()):
        process_date(date_str, video_files, root, args)

    print(f"\n{'='*60}")
    print("✅ 处理完成！")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
