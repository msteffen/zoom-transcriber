# Zoom transcription (macOS 14.2+)

Records speaker output and the default microphone independently using Apple's ScreenCaptureKit. After pressing Return, `record` (from `Recorder.swift`) records input and output audio, and `transcribe.py` transcribes the tracks and uploads the transcript to a Notion page. No video is saved.

## Prerequisites

- macOS 14.2+ (ScreenCaptureKit microphone stream support); command line developer tools (`xcode-select --install`)
- Homebrew `ffmpeg` (`brew install ffmpeg`)
- Python 3 and an OpenAI API key
- Notion integration token + page ID; share the parent page with your Notion integration, and give it permission to create content
- Screen & System Audio Recording permission and Microphone permission for the actual hosting process (typically Terminal or iTerm). Check System Settings > Privacy & Security. Restart Terminal if prompted.
- Use headphones to prevent mic picking up speaker audio, otherwise diarization accuracy suffers.

## Setup

```bash
./setup.sh
export OPENAI_API_KEY='...'
export NOTION_TOKEN='...'
export NOTION_PARENT_PAGE_ID='...'
./transcribe-zoom
```

Press Return at end of the call. Output is in `~/ZoomTranscripts/<timestamp>/` with `mic.m4a`, `system.m4a`, `timing.json`, `segments.json`, and `transcript.md`. A failed API step does not delete the recording: retry with:

```bash
.venv/bin/python transcribe.py "$HOME/ZoomTranscripts/<timestamp>" --no-notion
```

To use the newer transcription model:

```bash
.venv/bin/python transcribe.py "<recording directory>" --model gpt-transcribe --no-notion
```

**Model note:** `whisper-1` gives per-segment timestamps needed to merge the two sources into a chronological transcript, but it is deprecated and scheduled to shut down 2027-02-26. `gpt-transcribe` is recommended for new transcription work but does not presently offer this precise segmented-timestamp output; with that option this tool interleaves 10-minute chunks, not individual turns.

## Limitations / privacy

- No tested macOS environment was available to validate this prototype. Swift API details may require minor adjustment on your machine.
- System audio capture generally records all audible apps, not just the call. Pause notifications or other sound; use headphones to avoid double capture.
- Capture may be silent when the calling platform uses protected audio paths, or due to permissions.
- A stream can have a different starting timestamp or some drift. This tool uses CoreMedia sample PTS for initial alignment but does not correct subsequent drift.
- Whisper timestamps are approximate. Crosstalk and speech leakage between tracks can create duplicates, so review the transcript.
- The capture loop needs an interactive terminal; to run it unattended you'd need a dedicated app and lifecycle control.
- There is no duplicate-upload prevention if the Notion upload fails mid-way. Keep the local transcript as the source of truth.
- Audio is sent to OpenAI and text to Notion. Confirm your healthcare provider's consent, applicable recording law, and both services' privacy policies. Treat these as highly sensitive health records; consider local-only Whisper instead if cloud upload is unacceptable. Keep recordings in a private directory.
