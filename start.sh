#!/bin/bash
# 启动前重置终端，避免上次异常退出导致终端卡住
stty sane 2>/dev/null
cd "$(dirname "$0")"
source .venv/bin/activate

# web 模式：bash start.sh web
if [ "$1" = "web" ]; then
    exec python main_web.py
else
    exec python main.py "$@"
fi
