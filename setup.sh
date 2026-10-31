#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
command -v ffmpeg >/dev/null || { echo 'Install ffmpeg: brew install ffmpeg'; exit 1; }
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null ||
  { echo "Need Python 3.10+ (set PYTHON=/path/to/python3.x)"; exit 1; }

"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

printf '\nSetup complete. Configure OPENAI_API_KEY (and NOTION_TOKEN, NOTION_PARENT_PAGE_ID for --notion); run ./transcribe-zoom record\n'
