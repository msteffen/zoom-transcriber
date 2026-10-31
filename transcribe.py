#!/usr/bin/env python3
"""Transcribe two separately recorded streams and upload an optional Notion page."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

import requests

API = 'https://api.openai.com/v1/audio/transcriptions'
NOTION = 'https://api.notion.com/v1'


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def duration(path):
    return float(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], text=True).strip())


def transcribe(path, label, model):
    """Return relative-time segments for a single stream, with chunks <= 10 minutes."""
    results = []
    length = duration(path)
    with tempfile.TemporaryDirectory() as tmp:
        for n, start in enumerate(range(0, math.ceil(length), 600)):
            clip = Path(tmp) / f'{label}-{n:05}.mp3'
            run('ffmpeg',
                '-y', '-nostdin',
                '-i', str(path),
                '-ss', str(start),  # start time
                '-t', '600',  # maximum duration (last segment will be shorter)
                '-loglevel', 'error',
                '-ac', '1',
                '-ar', '16000',  # audio sampling frequency-whisper samples audio at 16kHz anyway
                '-b:a', '32k',  # bitrate
                str(clip))
            if clip.stat().st_size < 100: continue

            print(f'Transcribing {label} minutes {start // 60}-{min(length, start + 600) / 60:.1f}', flush=True)

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
                    if text:
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument('directory', type=Path)
    p.add_argument('--model', default='whisper-1', choices=['whisper-1', 'gpt-transcribe'])
    p.add_argument('--peer', default='Todd', help="The name used with the Zoom audio's transcript")
    p.add_argument('--me', default='Matthew', help="The name used with the mic audio's transcript")
    p.add_argument('--notion', action='store_true')
    p.add_argument('--title', default=None)
    args = p.parse_args()

    d = args.directory
    timing = json.loads((d / 'timing.json').read_text())
    # PTS values are from the same ScreenCaptureKit stream, but are not guaranteed
    # perfectly synchronized for all devices; allow checking manually.
    offsets = {}
    available = [timing.get('system_first_pts'), timing.get('mic_first_pts')]
    baseline = min(x for x in available if x is not None)
    for speaker in ('system', 'mic'):
        stamp = timing.get(f'{speaker}_first_pts')
        if stamp is None: raise RuntimeError(f'Missing {speaker} stream')
        offsets[speaker] = stamp - baseline
    segments = []
    for speaker, name in [('system', args.peer), ('mic', args.me)]:
        entries = transcribe(d / f'{speaker}.m4a', name, args.model)
        for item in entries: item['start'] += offsets[speaker]
        segments.extend(entries)

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

if __name__ == '__main__':
    main()
