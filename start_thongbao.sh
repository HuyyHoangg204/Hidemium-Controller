#!/bin/bash
# Chạy trong Git Bash:  bash start_thongbao.sh
# Hoặc daemon:          bash start_thongbao.sh --daemon

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Tìm node
NODE=""
for p in "/c/Program Files/nodejs/node.exe" "/c/Program Files (x86)/nodejs/node.exe" "$(which node 2>/dev/null)"; do
    if [ -f "$p" ] || command -v "$p" &>/dev/null 2>&1; then
        NODE="$p"
        break
    fi
done
NODE="${NODE:-node}"

# Tìm python có pymongo (ưu tiên venv)
PYTHON=""
for p in \
    "$SCRIPT_DIR/.venv/Scripts/python.exe" \
    "$SCRIPT_DIR/.venv/bin/python" \
    "$SCRIPT_DIR/venv/Scripts/python.exe" \
    "$SCRIPT_DIR/venv/bin/python" \
    "$(which python 2>/dev/null)" \
    "$(which python3 2>/dev/null)"; do
    if [ -f "$p" ] && "$p" -c "import pymongo" &>/dev/null 2>&1; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Khong tim thay Python co pymongo!"
    echo "Cai dat: .venv/Scripts/pip install pymongo"
    exit 1
fi

export PYTHON="$PYTHON"
export INTERVAL_MINUTES="${INTERVAL_MINUTES:-60}"

echo "=========================================="
echo " Thong bao Cookie Veo3"
echo " Python : $PYTHON"
echo " Node   : $NODE"
echo " Interval: ${INTERVAL_MINUTES} phut"
echo "=========================================="

"$NODE" thongbao.js --daemon "$@"
