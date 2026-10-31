#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

command -v swiftc >/dev/null || { echo 'Install Xcode command line tools: xcode-select --install'; exit 1; }
command -v ffmpeg >/dev/null || { echo 'Install ffmpeg: brew install ffmpeg'; exit 1; }

swiftc -parse-as-library \
  -O -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia Recorder.swift \
  -o recorder

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

printf '\nBuild complete. Configure OPENAI_API_KEY, NOTION_TOKEN, NOTION_PARENT_PAGE_ID; run ./transcribe-zoom\n'
