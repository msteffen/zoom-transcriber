#!/usr/bin/env python3
"""Record a Zoom call and/or transcribe both sides, optionally uploading to Notion."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import requests

API = 'https://api.openai.com/v1/audio/transcriptions'
NOTION = 'https://api.notion.com/v1'


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def peak_db(path):
    """Loudest sample in dBFS (about -91 for digital silence)."""
    out = subprocess.run(['ffmpeg', '-nostdin', '-i', str(path), '-af', 'volumedetect', '-f', 'null', '-'],
                         check=True, capture_output=True, text=True).stderr
    value = out.rsplit('max_volume:', 1)[1].split()[0]
    return float('-inf') if value == '-inf' else float(value)


def hallucinated(segment):
    # Whisper tends to invent filler ("you", "Thank you.") over silence. These are the
    # thresholds openai/whisper itself uses to discard such segments.
    return segment.get('no_speech_prob', 0) > 0.6 and segment.get('avg_logprob', 0) < -1


def transcribe(path, label, model):
    """Return relative-time segments for a single stream, with chunks <= 10 minutes."""
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        # Let ffmpeg do the chunking: a recording cut short by a crash has no duration
        # in its header, but still decodes fine.
        run('ffmpeg', '-nostdin', '-loglevel', 'error', '-i', str(path),
            '-ac', '1',
            '-ar', '16000',  # audio sampling frequency-whisper samples audio at 16kHz anyway
            '-b:a', '32k',  # bitrate
            '-f', 'segment', '-segment_time', '600', str(Path(tmp) / f'{label}-%05d.mp3'))
        for n, clip in enumerate(sorted(Path(tmp).glob('*.mp3'))):
            start = n * 600
            if clip.stat().st_size < 100: continue
            if peak_db(clip) < -60:
                print(f'Skipping {label} minutes {start // 60}-{start // 60 + 10} (silent)', flush=True)
                continue

            print(f'Transcribing {label} minutes {start // 60}-{start // 60 + 10}', flush=True)

            headers = {'Authorization': f'Bearer {os.environ["OPENAI_API_KEY"]}'}
            data = {'model': model}
            if model == 'whisper-1':
                data.update({'response_format': 'verbose_json', 'timestamp_granularities[]': 'segment'})
            with clip.open('rb') as f:
                r = requests.post(API, headers=headers, data=data,
                                  files={'file': (clip.name, f, 'audio/mpeg')}, timeout=900)
            r.raise_for_status()
            result = r.json()
            if model == 'whisper-1':
                for segment in result.get('segments', []):
                    text = segment['text'].strip()
                    if text and not hallucinated(segment):
                        results.append({'start': start + segment['start'], 'speaker': label, 'text': text})
            else:
                text = result.get('text', '').strip()
                if text:
                    results.append({'start': start, 'speaker': label,
                                    'text': text, 'chunk': True})
    return results


def clock(seconds):
    seconds = int(max(seconds, 0))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def notion_request(method, path, payload):
    headers = {'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
               'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}
    for attempt in range(6):
        r = requests.request(method, NOTION + path, headers=headers, json=payload, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.headers.get('Retry-After', '2')))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError('Notion API rate limited repeatedly')


def rich_block(text):
    # Keep each rich_text content well below Notion's 2000-character limit.
    spans = [text[i:i + 1800] for i in range(0, len(text), 1800)]
    return {'object': 'block', 'type': 'paragraph',
            'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': s}} for s in spans]}}


def upload(title, lines):
    parent = os.environ['NOTION_PARENT_PAGE_ID'].replace('-', '')
    page = notion_request('POST', '/pages', {
        'parent': {'type': 'page_id', 'page_id': parent},
        'properties': {'title': {'type': 'title', 'title': [{'text': {'content': title}}]}}
    })
    blocks = [rich_block(line) for line in lines]
    for n in range(0, len(blocks), 75):
        notion_request('PATCH', f'/blocks/{page["id"]}/children', {'children': blocks[n:n + 75]})
    return page['url']


def record(directory):
    import capture_macos  # imported here so transcribing alone works on any OS

    stop = threading.Event()
    # Return stops the recording; so do Ctrl-C and SIGTERM, after the files are closed cleanly.
    if sys.stdin.isatty():
        threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
    previous = {sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        recorded = capture_macos.record(directory, stop.is_set)
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)
    if recorded: print(f'Saved mic.flac and system.flac to {directory}', flush=True)
    return recorded


def transcribe_directory(d, args):
    segments = []
    for speaker, name in [('system', args.peer), ('mic', args.me)]:
        segments.extend(transcribe(d / f'{speaker}.flac', name, args.model))

    segments.sort(key=lambda x: x['start'])
    title = args.title or f'Transcript — {dt.date.today().isoformat()}'
    lines = [f'[{clock(s["start"])}] **{s["speaker"]}:** {s["text"]}' for s in segments]
    if args.model == 'gpt-transcribe':
        note = '> Timing is approximate: each entry represents an entire 10-minute chunk, not a speech turn.'
        lines.insert(0, note)
    md = '# ' + title + '\n\n' + '\n\n'.join(lines) + '\n'
    out = d / 'transcript.md'
    out.write_text(md)
    (d / 'segments.json').write_text(json.dumps(segments, indent=2))
    print(f'Local transcript: {out}')
    if args.notion:
        print('Notion:', upload(title, lines))


def main():
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument('--model', default='whisper-1', choices=['whisper-1', 'gpt-transcribe'])
    options.add_argument('--peer', default='Todd', help="The name used with the Zoom audio's transcript")
    options.add_argument('--me', default='Matthew', help="The name used with the mic audio's transcript")
    options.add_argument('--notion', action='store_true')
    options.add_argument('--title', default=None)

    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    rec = commands.add_parser('record', parents=[options],
                              help='Record the next Zoom meeting (macOS), then transcribe it')
    rec.add_argument('directory', type=Path, nargs='?',
                     default=Path.home() / 'Transcripts' / dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S'),
                     help='Where to save the recording (default: ~/Transcripts/<timestamp>)')
    tr = commands.add_parser('transcribe', parents=[options], help='Transcribe an existing recording')
    tr.add_argument('directory', type=Path)

    # Make 'record' the default verb (argparse can't do default commands)
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith('-') and argv[0] not in ('-h', '--help')):
        argv.insert(0, 'record')

    # Parse modified arguments
    args = p.parse_args(argv)

    d = args.directory
    if args.command == 'record':
        if any(d.glob('*.flac')):
            rec.error(f'{d} already has a recording; use `transcribe {d}` to transcribe it')
        d.mkdir(parents=True, exist_ok=True)
        if not record(d):
            return
    transcribe_directory(d, args)


if __name__ == '__main__':
    main()
