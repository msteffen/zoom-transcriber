"""Record Zoom's audio output and the microphone on macOS (14.2+), via a Core Audio process tap.

The mic and the tap are recorded as two streams, started together. (Combining them
in one aggregate device would share a clock, but loses half the samples when their
rates differ, e.g. a 24 kHz Bluetooth headset mic vs. the 48 kHz tap.) The streams
start a few ms apart and drift well under a second per hour, which is fine for a
transcript. Audio is streamed to FLAC as it arrives, so a crash, kill, or dead
battery loses at most the last block.
"""
import contextlib
import struct
import threading
import time
import uuid

import CoreAudio as CA
import Foundation
import objc
import sounddevice as sd
import soundfile as sf

ZOOM = 'us.zoom.xos'
AGGREGATE_NAME = 'Recorder Zoom Tap'
_GLOBAL = CA.kAudioObjectPropertyScopeGlobal
_MAIN = CA.kAudioObjectPropertyElementMain


def _get(obj, selector, size=8, qualifier=None):
    addr = CA.AudioObjectPropertyAddress(selector, _GLOBAL, _MAIN)
    q = qualifier if qualifier is not None else objc.NULL
    err, n, data = CA.AudioObjectGetPropertyData(obj, addr, len(qualifier or b''), q, size, None)
    if err: raise OSError(f'Core Audio property {selector:#x} on object {obj}: OSStatus {err}')
    return bytes(data)[:n]


def _u32(obj, selector):
    return struct.unpack('I', _get(obj, selector, 4))[0]


def _cfstring(obj, selector):
    ptr = struct.unpack('Q', _get(obj, selector))[0]
    return str(objc.objc_object(c_void_p=ptr)) if ptr else ''


def _key(k):
    # PyObjC exposes these C string constants as bytes; Core Audio crashes on non-string keys.
    return k.decode() if isinstance(k, bytes) else k


def _zoom_processes():
    data = _get(CA.kAudioObjectSystemObject, CA.kAudioHardwarePropertyProcessObjectList, 4096)
    ids = struct.unpack(f'{len(data) // 4}I', data)
    return [p for p in ids if _cfstring(p, CA.kAudioProcessPropertyBundleID) == ZOOM]


def zoom_in_meeting():
    """Zoom opens the microphone when a meeting starts and releases it when the meeting ends."""
    return any(_u32(p, CA.kAudioProcessPropertyIsRunningInput) for p in _zoom_processes())


def _zoom_tap_description():
    desc = CA.CATapDescription.alloc().initStereoMixdownOfProcesses_([])
    if desc.respondsToSelector_('setBundleIDs:'):  # macOS 26: follows Zoom even if it restarts
        desc.setBundleIDs_([ZOOM])
        desc.setProcessRestoreEnabled_(True)
        return desc
    procs = _zoom_processes()
    if not procs: raise RuntimeError('Zoom is not running')
    return CA.CATapDescription.alloc().initStereoMixdownOfProcesses_(procs)


@contextlib.contextmanager
def _zoom_tap_device():
    """Yield the sounddevice index of a private aggregate device wrapping a Zoom output tap."""
    desc = _zoom_tap_description()
    desc.setName_(AGGREGATE_NAME)
    desc.setPrivate_(True)
    err, tap = CA.AudioHardwareCreateProcessTap(desc, None)
    if err: raise OSError(f'Could not create Zoom audio tap (OSStatus {err})')
    try:
        spec = Foundation.NSDictionary.dictionaryWithDictionary_({
            _key(CA.kAudioAggregateDeviceNameKey): AGGREGATE_NAME,
            _key(CA.kAudioAggregateDeviceUIDKey): f'recorder.{uuid.uuid4()}',
            _key(CA.kAudioAggregateDeviceIsPrivateKey): True,  # vanishes if this process dies
            _key(CA.kAudioAggregateDeviceTapListKey): [{_key(CA.kAudioSubTapUIDKey): desc.UUID().UUIDString()}],
            _key(CA.kAudioAggregateDeviceTapAutoStartKey): True,
        })
        err, aggregate = CA.AudioHardwareCreateAggregateDevice(spec, None)
        if err: raise OSError(f'Could not create aggregate audio device (OSStatus {err})')
        try:
            # PortAudio only enumerates devices when initialized, so rescan now that ours exists.
            sd._terminate(); sd._initialize()
            yield next(i for i, d in enumerate(sd.query_devices()) if d['name'] == AGGREGATE_NAME)
        finally:
            CA.AudioHardwareDestroyAggregateDevice(aggregate)
    finally:
        CA.AudioHardwareDestroyProcessTap(tap)


@contextlib.contextmanager
def _default_mic_stream():
    info = sd.query_devices(kind='input')  # after _zoom_tap_device's rescan, so the index is current
    with sd.InputStream(device=info['index'], channels=info['max_input_channels'],
                        samplerate=info['default_samplerate'], dtype='float32', latency='high') as stream:
        yield stream, info


def _stream_to_flac(stream, path, channels, stop, peaks, block_seconds=0.5):
    """Copy a started InputStream to a FLAC file until stop is set, downmixing to `channels`.

    Records the loudest sample in peaks[path.stem].
    """
    peaks[path.stem] = 0.0
    block = int(stream.samplerate * block_seconds)
    with sf.SoundFile(path, 'w', int(stream.samplerate), channels) as out:
        while not stop.is_set():
            try:
                frames, overflowed = stream.read(block)
            except sd.PortAudioError as e:  # e.g. the mic was unplugged, or the Mac slept
                print(f'Audio device error on {path.name}, stopping: {e}', flush=True)
                stop.set()
                break
            if overflowed: print(f'Warning: dropped some {path.stem} audio (input overflow)', flush=True)
            out.write(frames.mean(axis=1, keepdims=True) if channels == 1 else frames)
            peaks[path.stem] = max(peaks[path.stem], float(abs(frames).max(initial=0)))
            out.flush()


def record(directory, stop_requested):
    """Write mic.flac and system.flac to directory.

    Waits for a Zoom meeting to start, then records until the meeting ends or
    stop_requested() returns True.
    """
    if not zoom_in_meeting():
        print('Waiting for a Zoom meeting to start (press Return to cancel)...', flush=True)
        while not zoom_in_meeting():
            if stop_requested(): return False
            time.sleep(1)
    with _zoom_tap_device() as tap_device, \
            _default_mic_stream() as (mic, mic_info), \
            sd.InputStream(device=tap_device, channels=2,
                           samplerate=sd.query_devices(tap_device)['default_samplerate'],
                           dtype='float32', latency='high') as system:
        print(f'RECORDING mic "{mic_info["name"]}" and Zoom output. '
              'Stops automatically when the meeting ends, or press Return.', flush=True)
        stop = threading.Event()
        peaks = {}
        writers = [threading.Thread(target=_stream_to_flac, args=(mic, directory / 'mic.flac', 1, stop, peaks)),
                   threading.Thread(target=_stream_to_flac, args=(system, directory / 'system.flac', 2, stop, peaks))]
        for w in writers: w.start()
        while not stop.is_set() and not stop_requested():
            if not zoom_in_meeting():
                print('Zoom meeting ended.', flush=True)
                break
            time.sleep(0.5)
        stop.set()
        for w in writers: w.join()
    # Without System Audio Recording permission, a tap delivers exact zeros instead of failing.
    if peaks.get('system') == 0.0:
        print('Warning: the Zoom track is completely silent. Check that your terminal app has '
              'System Audio Recording permission (System Settings > Privacy & Security).', flush=True)
    return True
