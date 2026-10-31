# Zoom call transcription (macOS 14.2+)

Waits for a Zoom meeting to start, records Zoom's audio output and your microphone as separate tracks, stops automatically when the meeting ends, then transcribes both tracks with OpenAI and (optionally) creates a Notion page.

Zoom's audio is captured with a Core Audio process tap, which only needs the "System Audio Recording Only" permission (no screen recording), and only hears Zoom: notification sounds and other apps aren't recorded.

## Prerequisites

- macOS 14.2+ (tested on macOS 26)
- Python 3.10+ and Homebrew `ffmpeg` (`brew install ffmpeg`)
- An OpenAI API key; for `--notion`, a Notion integration token + parent page ID (share the page with the integration)
- Microphone and System Audio Recording permission for your terminal app (System Settings > Privacy & Security). macOS prompts the first time.
- Use headphones, so the mic doesn't also pick up the other person.

## Setup

```bash
PYTHON=python3.14 ./setup.sh   # any Python 3.10+
export OPENAI_API_KEY='...'
export NOTION_TOKEN='...'
export NOTION_PARENT_PAGE_ID='...'
./transcribe-zoom record
```

`transcribe-zoom record` can be started before the call: it waits until Zoom turns on its microphone (i.e. you've joined a meeting), records until Zoom releases the mic (the meeting ends), then transcribes. If a meeting is already going, it starts recording right away. Press Return (or Ctrl-C) to stop early.

Output is in `~/Transcripts/<timestamp>/` (or pass a directory: `transcribe-zoom record DIR`): `mic.flac`, `system.flac`, `segments.json`, and `transcript.md`. A failed API step does not delete the recording; retry with:

```bash
./transcribe-zoom transcribe ~/Transcripts/<timestamp>
```

Add `--notion` to upload to Notion, or `--model gpt-transcribe` for the newer transcription model.

**Model note:** `whisper-1` gives per-segment timestamps needed to merge the two sources into a chronological transcript, but it is deprecated and scheduled to shut down 2027-02-26. `gpt-transcribe` does not presently offer segment timestamps; with that option this tool interleaves 10-minute chunks, not individual turns.

## Robustness

- Audio is written to FLAC every half second, so a crash, `kill -9`, or dead battery loses at most the last moment of audio, and the partial file still transcribes.
- Return, Ctrl-C, closing the terminal (SIGHUP), and SIGTERM all stop the recording cleanly before exiting.
- If the Mac sleeps mid-call, recording pauses with it. Zoom usually drops the call, so on wake the recording stops (or on an audio device error) and transcribes what was captured.

## Limitations / privacy

- The mic is whichever input is the default when recording starts. Switching mics mid-call (e.g. connecting AirPods) isn't followed.
- The two tracks have separate clocks: they start a few milliseconds apart and drift well under a second per hour. Fine for a transcript, not for audio editing.
- Meeting detection relies on Zoom keeping the mic open during a meeting. If Zoom releases it for long stretches (e.g. while muted, on some setups), the recording would stop early. Briefly switching audio devices mid-call (e.g. AirPods connecting) may also end it.
- Whisper timestamps are approximate. Crosstalk and speech leakage between tracks can create duplicates, so review the transcript.
- Audio is sent to OpenAI and, with `--notion`, text to Notion. Confirm consent, applicable recording law, and both services' privacy policies. Treat these as sensitive health records; consider local-only Whisper instead if cloud upload is unacceptable. Keep recordings in a private directory.
