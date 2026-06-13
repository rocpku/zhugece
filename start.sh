#!/bin/bash
# 启动前重置终端，避免上次异常退出导致终端卡住
stty sane 2>/dev/null
cd "$(dirname "$0")"

# 激活虚拟环境（如果存在）
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
    source venv/bin/activate
elif ! python3 -c "import openai" 2>/dev/null; then
    echo "请先安装依赖: pip install -r requirements.txt"
    exit 1
fi

exec python main_web.py
