#!/bin/bash
# Garden AutoCut 启动脚本
# 用法: ./scripts/start.sh [--port PORT] [--data-dir DIR]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PORT="${PORT:-8766}"
DATA_DIR="${DATA_DIR:-$HOME/Movies/GardenAutoCut}"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# 查找 Python
if [ -f "/Users/zhkn/workspace/hermes/download/hermes-agent-main/venv/bin/python3" ]; then
    PYTHON="/Users/zhkn/workspace/hermes/download/hermes-agent-main/venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "❌ 找不到 Python3"
    exit 1
fi

echo "🌿 Garden AutoCut"
echo "   Python: $PYTHON"
echo "   Port:   $PORT"
echo "   Data:   $DATA_DIR"
echo ""

cd "$PROJECT_DIR"
exec "$PYTHON" -m app.server --port "$PORT" --data-dir "$DATA_DIR"
