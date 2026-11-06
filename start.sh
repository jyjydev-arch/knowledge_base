#!/usr/bin/env bash
# 在项目根目录启动后端（app 作为包的父目录必须在 sys.path 里）
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
