import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// Each stream gets its own m4a writer. We retain the initial CoreMedia timestamp
// so a downstream tool can align the two recordings.
final class AudioSink {
    let url: URL
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    private(set) var firstPTS: Double?
    private(set) var count = 0
    private let lock = NSLock()

    init(url: URL, sample: CMSampleBuffer) throws {
        self.url = url
        guard let fmt = CMSampleBufferGetFormatDescription(sample),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee else {
            throw NSError(domain: "Recorder", code: 1, userInfo: [NSLocalizedDescriptionKey: "Missing audio format"])
        }
        fputs("\(url.lastPathComponent) source format: \(asbd.mSampleRate) Hz, \(asbd.mChannelsPerFrame) ch, formatID \(asbd.mFormatID), flags \(asbd.mFormatFlags)\n", stderr)
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .m4a)
        // No fixed bitrate: valid AAC bitrates depend on the input rate (e.g. 96 kbps
        // fails for a 16/24 kHz Bluetooth headset mic), so let the encoder choose.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: asbd.mSampleRate,
            AVNumberOfChannelsKey: Int(asbd.mChannelsPerFrame)
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings, sourceFormatHint: fmt)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw NSError(domain: "Recorder", code: 2) }
        writer.add(input)
        guard writer.startWriting() else { throw writer.error ?? NSError(domain: "Recorder", code: 3) }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        writer.startSession(atSourceTime: pts)
        firstPTS = CMTimeGetSeconds(pts)
    }

    func append(_ sample: CMSampleBuffer) {
        lock.lock(); defer { lock.unlock() }
        if input.isReadyForMoreMediaData {
            if input.append(sample) { count += 1 }
            else { fputs("Audio write error: \(writer.error?.localizedDescription ?? "unknown")\n", stderr) }
        } else {
            fputs("Warning: dropped an audio sample due to encoder backpressure\n", stderr)
        }
    }

    func finish() async {
        lock.withLock {
            input.markAsFinished()
        }
        await withCheckedContinuation { continuation in
            writer.finishWriting { continuation.resume() }
        }
        if let e = writer.error { fputs("Finalization error for \(url.lastPathComponent): \(e)\n", stderr) }
    }
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var system: AudioSink?
    private var mic: AudioSink?
    private let directory: URL
    private let queue = DispatchQueue(label: "capture.serial.audio")
    private var stream: SCStream?
    private(set) var failure: String?
    private var failedSinks = Set<String>()

    init(directory: URL) { self.directory = directory }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard sample.isValid else { return }
        switch type {
        case .audio, .microphone:
            let isMic = type == .microphone
            let name = isMic ? "mic" : "system"
            // If creating this stream's writer failed once, don't retry on every sample.
            guard !failedSinks.contains(name) else { return }
            do {
                if isMic {
                    if mic == nil { mic = try AudioSink(url: directory.appendingPathComponent("mic.m4a"), sample: sample) }
                    mic?.append(sample)
                } else {
                    if system == nil { system = try AudioSink(url: directory.appendingPathComponent("system.m4a"), sample: sample) }
                    system?.append(sample)
                }
            } catch {
                failedSinks.insert(name)
                failure = error.localizedDescription
                fputs("Capture error (\(name)): \(error)\n", stderr)
            }
        default: break
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        failure = error.localizedDescription
        fputs("Stream stopped: \(error)\n", stderr)
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw NSError(domain: "Recorder", code: 4, userInfo: [NSLocalizedDescriptionKey: "No display found"]) }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.captureMicrophone = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // No video is saved. The capture stream still uses a display filter.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let s = SCStream(filter: filter, configuration: config, delegate: self)
        stream = s
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        try await s.startCapture()
    }

    func stop() async throws {
        if let s = stream { try await s.stopCapture() }
        // Drain the serial callback queue before closing the asset writers.
        await withCheckedContinuation { c in queue.async { c.resume() } }
        let sys = system, microphone = mic
        if let sys { await sys.finish() }
        if let microphone { await microphone.finish() }
        let info: [String: Any] = [
            "system_first_pts": sys?.firstPTS as Any? ?? NSNull(),
            "mic_first_pts": microphone?.firstPTS as Any? ?? NSNull(),
            "system_samples": sys?.count ?? 0,
            "mic_samples": microphone?.count ?? 0
        ]
        try JSONSerialization.data(withJSONObject: info, options: [.prettyPrinted, .sortedKeys])
            .write(to: directory.appendingPathComponent("timing.json"))
        if sys == nil || microphone == nil {
            throw NSError(domain: "Recorder", code: 5, userInfo: [NSLocalizedDescriptionKey: "Missing system or microphone audio. Check permissions / active sound input."])
        }
        if let failure { throw NSError(domain: "Recorder", code: 6, userInfo: [NSLocalizedDescriptionKey: failure]) }
    }
}

@main struct Main {
    static func main() async {
        do {
            guard CommandLine.arguments.count == 2 else {
                fputs("Usage: recorder OUTPUT_DIRECTORY\n", stderr); exit(2)
            }
            let dir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let recorder = Capture(directory: dir)
            try await recorder.start()
            print("RECORDING. Press Return to stop. (Keep the call audio playing on this Mac.)")
            _ = readLine()
            try await recorder.stop()
            print("Saved mic.m4a and system.m4a to \(dir.path)")
        } catch {
            fputs("Recorder failed: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
