"""
JSON 工具 — 校验加载 + 原子写入
替代 server.py 中散落的 json.load / json.dump 调用。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Type, TypeVar

T = TypeVar("T")


def load_json(path: str | Path, default=None):
    """加载 JSON 文件，不存在或解析失败时返回 default"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  load_json 失败 {path.name}: {e}")
        return default


def load_validated(path: str | Path, model: Type[T], default: Optional[T] = None) -> Optional[T]:
    """加载 JSON 并用 Pydantic model 校验。校验失败返回 default。"""
    data = load_json(path)
    if data is None:
        return default
    try:
        return model(**data)
    except Exception as e:
        print(f"  load_validated 校验失败 {Path(path).name}: {e}")
        # 尝试宽松加载
        if hasattr(model, 'model_validate'):
            try:
                return model.model_validate(data, strict=False)
            except Exception:
                pass
        return default


def atomic_write_json(path: str | Path, data, indent: int = 2) -> str:
    """原子写入 JSON：先写临时文件，再 rename，避免写入中断导致文件损坏"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 写入临时文件
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        # 原子替换
        os.replace(tmp_path, str(path))
    except Exception:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(path)


def extract_json_from_text(text: str):
    """从 AI 返回的文本中提取最外层 JSON 对象/数组，健壮的括号匹配"""
    # 优先找对象
    depth = 0
    start = -1
    for ci, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = ci
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:ci+1])
                except json.JSONDecodeError:
                    start = -1

    # 再找数组
    depth = 0
    start = -1
    for ci, ch in enumerate(text):
        if ch == '[':
            if depth == 0:
                start = ci
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:ci+1])
                except json.JSONDecodeError:
                    start = -1

    return None
