#!/bin/bash
# Mac：雙擊執行。第一次會自動安裝所需套件。
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --clear .venv && .venv/bin/pip install -r requirements.txt || { echo "安裝失敗，請確認已安裝 Python 3"; read -r; exit 1; }
fi
(sleep 2; open http://localhost:8000) &
.venv/bin/python app.py
