"""Optional HEVC re-encode pass for the GUI worker.

Uses `AVAssetReader` + `AVAssetWriter` to re-encode the source video to
HEVC at a user-chosen bitrate, preserving the source's HDR colour metadata
(HLG, HDR10/PQ tags). Audio is passed through under its original codec.

Apple's Dolby Vision RPU is encoded inside the HEVC bitstream itself and
cannot be carried through a re-encode without explicit Dolby Vision
support — the output will be plain HEVC with HLG/HDR10/SDR colour tags.
HLG and HDR10 transfer + colour primaries are propagated.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time

import AVFoundation
import CoreMedia
import Quartz  # exposes CoreVideo constants (kCVPixelBufferPixelFormatTypeKey, etc.)
import objc
from Foundation import NSURL


# ---------- source colour-tag extraction ----------


def _extract_color_properties(video_track) -> dict | None:
    """Read colour metadata from a video track's first format description."""
    descs = video_track.formatDescriptions()
    if not descs:
        return None
    extensions = CoreMedia.CMFormatDescriptionGetExtensions(descs[0])
    if not extensions:
        return None

    color_primaries = extensions.get(
        CoreMedia.kCMFormatDescriptionExtension_ColorPrimaries
    )
    transfer = extensions.get(
        CoreMedia.kCMFormatDescriptionExtension_TransferFunction
    )
    ycbcr_matrix = extensions.get(
        CoreMedia.kCMFormatDescriptionExtension_YCbCrMatrix
    )

    props: dict = {}
    if color_primaries:
        props[AVFoundation.AVVideoColorPrimariesKey] = color_primaries
    if transfer:
        props[AVFoundation.AVVideoTransferFunctionKey] = transfer
    if ycbcr_matrix:
        props[AVFoundation.AVVideoYCbCrMatrixKey] = ycbcr_matrix
    return props or None


def _is_hdr_transfer(transfer) -> bool:
    return transfer in (
        AVFoundation.AVVideoTransferFunction_ITU_R_2100_HLG,
        AVFoundation.AVVideoTransferFunction_SMPTE_ST_2084_PQ,
    )


def _audio_subtype(audio_track) -> int | None:
    """Read the audio track's CoreMedia subtype (a FourCC packed as int)."""
    descs = audio_track.formatDescriptions()
    if not descs:
        return None
    return CoreMedia.CMFormatDescriptionGetMediaSubType(descs[0])


def _fourcc_to_str(code: int) -> str:
    """Render a FourCC int as 'abcd' (e.g., 'aac ' or 'lpcm'), stripping nulls."""
    if not code:
        return "?"
    try:
        b = code.to_bytes(4, "big")
        return b.decode("ascii", errors="replace").strip().strip("\x00") or hex(code)
    except Exception:
        return hex(code)


def _is_aac_audio(audio_track) -> bool:
    return _audio_subtype(audio_track) == AVFoundation.kAudioFormatMPEG4AAC


def _pixel_format_for_source(video_track, hdr: bool) -> int:
    """Choose a decoder output pixel format that matches the source's depth.

    HDR sources need 10-bit so the encoder doesn't quantise highlights to 8-bit.
    """
    if hdr:
        return Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
    return Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange


# ---------- pump loop ----------


def _pump(reader_output, writer_input, writer, done_event):
    """Move sample buffers from a reader output to a writer input.

    Polls `isReadyForMoreMediaData` (rather than using
    requestMediaDataWhenReadyOnQueue:, which is awkward from PyObjC) and
    signals `done_event` when the input is exhausted.

    Bails out as soon as the writer transitions into the Failed state so we
    don't append additional samples to a writer that's about to assert in
    native code — that's the path that aborts the whole process rather than
    surfacing a Python exception.
    """
    failed_status = AVFoundation.AVAssetWriterStatusFailed
    try:
        while True:
            if writer.status() == failed_status:
                return
            if not writer_input.isReadyForMoreMediaData():
                time.sleep(0.002)
                continue
            sample = reader_output.copyNextSampleBuffer()
            if sample is None:
                writer_input.markAsFinished()
                return
            try:
                ok = writer_input.appendSampleBuffer_(sample)
            except Exception:
                # Native NSException bridged into Python — bail.
                writer_input.markAsFinished()
                return
            if not ok:
                writer_input.markAsFinished()
                return
    finally:
        done_event.set()


# ---------- public entry point ----------


def _av_convert(
    source_path: str | os.PathLike,
    dest_path: str | os.PathLike,
    *,
    video_codec: str | None,
    video_bitrate: int | None,
) -> None:
    """Internal AVAssetReader + AVAssetWriter pipeline.

    `video_codec=None` ⇒ video stream is copied through byte-perfect (no
    decode, no re-encode) via a sourceFormatHint passthrough writer input.
    `video_codec="hvc1"` (or any AVVideoCodecType) ⇒ video is decoded and
    re-encoded at `video_bitrate`, with HDR colour tags propagated and the
    HEVC Main10 profile requested for HDR sources.

    Audio is always normalised to AAC when the source isn't already AAC
    (so PCM, AC-3, etc. are auto-fixed for Photos / Live-Photo import);
    AAC sources pass through unchanged.
    """
    src = pathlib.Path(source_path)
    dst = pathlib.Path(dest_path)
    if dst.exists():
        dst.unlink()

    is_passthrough_video = video_codec is None

    with objc.autorelease_pool():
        asset = AVFoundation.AVAsset.assetWithURL_(NSURL.fileURLWithPath_(str(src)))
        video_tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
        if not video_tracks:
            raise ValueError(f"{src}: no video track")
        video_track = video_tracks[0]
        audio_tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeAudio) or []

        natural_size = video_track.naturalSize()
        width = int(natural_size.width)
        height = int(natural_size.height)
        transform = video_track.preferredTransform()

        # Colour-property extraction is only meaningful when we're going to
        # re-encode (passthrough preserves whatever the source carried).
        if is_passthrough_video:
            color_props = None
            hdr = False
        else:
            color_props = _extract_color_properties(video_track)
            hdr = color_props is not None and _is_hdr_transfer(
                color_props.get(AVFoundation.AVVideoTransferFunctionKey)
            )

        # --- Reader ---
        reader, err = AVFoundation.AVAssetReader.alloc().initWithAsset_error_(asset, None)
        if reader is None:
            raise RuntimeError(f"AVAssetReader init failed: {err}")

        if is_passthrough_video:
            # outputSettings=None ⇒ deliver compressed video samples; the
            # writer's sourceFormatHint mode will round-trip them as-is.
            video_reader_output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
                video_track, None
            )
        else:
            pixel_format = _pixel_format_for_source(video_track, hdr)
            video_reader_output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
                video_track,
                {Quartz.kCVPixelBufferPixelFormatTypeKey: pixel_format},
            )
        video_reader_output.setAlwaysCopiesSampleData_(False)
        reader.addOutput_(video_reader_output)

        audio_reader_output = None
        audio_needs_transcode = False
        if audio_tracks:
            audio_needs_transcode = not _is_aac_audio(audio_tracks[0])
            if audio_needs_transcode:
                # Apple Photos requires AAC audio in Live-Photo paired videos.
                # PCM / AC-3 / other codecs cause PhotoKit to silently reject the
                # asset. Decode to interleaved 16-bit PCM here, then encode the
                # writer side as AAC.
                audio_reader_output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
                    audio_tracks[0],
                    {
                        AVFoundation.AVFormatIDKey: AVFoundation.kAudioFormatLinearPCM,
                        AVFoundation.AVLinearPCMBitDepthKey: 16,
                        AVFoundation.AVLinearPCMIsBigEndianKey: False,
                        AVFoundation.AVLinearPCMIsFloatKey: False,
                        AVFoundation.AVLinearPCMIsNonInterleaved: False,
                    },
                )
            else:
                # AAC source — passthrough.
                audio_reader_output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
                    audio_tracks[0], None
                )
            audio_reader_output.setAlwaysCopiesSampleData_(False)
            reader.addOutput_(audio_reader_output)

        # --- Writer ---
        writer, err = AVFoundation.AVAssetWriter.alloc().initWithURL_fileType_error_(
            NSURL.fileURLWithPath_(str(dst)),
            AVFoundation.AVFileTypeQuickTimeMovie,
            None,
        )
        if writer is None:
            raise RuntimeError(f"AVAssetWriter init failed: {err}")

        if is_passthrough_video:
            # Passthrough — give the writer the source's format description so
            # it knows how to wrap the compressed samples without re-encoding.
            fmt_descs = video_track.formatDescriptions()
            src_fmt = fmt_descs[0] if fmt_descs else None
            video_writer_input = AVFoundation.AVAssetWriterInput.alloc().initWithMediaType_outputSettings_sourceFormatHint_(
                AVFoundation.AVMediaTypeVideo, None, src_fmt
            )
        else:
            compression: dict = {AVFoundation.AVVideoAverageBitRateKey: int(video_bitrate)}
            if hdr:
                profile_key = getattr(
                    AVFoundation, "kVTProfileLevel_HEVC_Main10_AutoLevel", None
                )
                if profile_key is not None:
                    compression[AVFoundation.AVVideoProfileLevelKey] = profile_key

            video_writer_settings: dict = {
                AVFoundation.AVVideoCodecKey: video_codec,
                AVFoundation.AVVideoWidthKey: width,
                AVFoundation.AVVideoHeightKey: height,
                AVFoundation.AVVideoCompressionPropertiesKey: compression,
            }
            if color_props is not None:
                video_writer_settings[AVFoundation.AVVideoColorPropertiesKey] = color_props

            video_writer_input = AVFoundation.AVAssetWriterInput.alloc().initWithMediaType_outputSettings_(
                AVFoundation.AVMediaTypeVideo, video_writer_settings
            )
        video_writer_input.setExpectsMediaDataInRealTime_(False)
        video_writer_input.setTransform_(transform)
        writer.addInput_(video_writer_input)

        audio_writer_input = None
        if audio_reader_output is not None:
            if audio_needs_transcode:
                src_sr = float(audio_tracks[0].naturalTimeScale() or 44100)
                # AVAudioFormat-style settings dict; the writer routes this to
                # the AAC encoder.
                audio_writer_input = AVFoundation.AVAssetWriterInput.alloc().initWithMediaType_outputSettings_(
                    AVFoundation.AVMediaTypeAudio,
                    {
                        AVFoundation.AVFormatIDKey: AVFoundation.kAudioFormatMPEG4AAC,
                        AVFoundation.AVSampleRateKey: 48000.0 if src_sr <= 0 else min(48000.0, src_sr),
                        AVFoundation.AVNumberOfChannelsKey: 2,
                        AVFoundation.AVEncoderBitRateKey: 192_000,
                    },
                )
            else:
                # AAC source — passthrough.
                audio_writer_input = AVFoundation.AVAssetWriterInput.alloc().initWithMediaType_outputSettings_(
                    AVFoundation.AVMediaTypeAudio, None
                )
            audio_writer_input.setExpectsMediaDataInRealTime_(False)
            writer.addInput_(audio_writer_input)

        # --- Start ---
        if not reader.startReading():
            raise RuntimeError(f"reader.startReading: {reader.error()}")
        if not writer.startWriting():
            raise RuntimeError(f"writer.startWriting: {writer.error()}")
        try:
            from CoreMedia import CMTimeMake
            writer.startSessionAtSourceTime_(CMTimeMake(0, 1))
        except ImportError:
            # CMTime may not be importable as a callable; alternative path:
            writer.startSessionAtSourceTime_((0, 1, 0, 0))

        # --- Pump video + audio on separate threads ---
        video_done = threading.Event()
        audio_done = threading.Event()
        threads = [threading.Thread(
            target=_pump,
            args=(video_reader_output, video_writer_input, writer, video_done),
            daemon=True,
        )]
        threads[0].start()
        if audio_writer_input is not None:
            t = threading.Thread(
                target=_pump,
                args=(audio_reader_output, audio_writer_input, writer, audio_done),
                daemon=True,
            )
            t.start()
            threads.append(t)
        else:
            audio_done.set()

        for t in threads:
            t.join()
        video_done.wait()
        audio_done.wait()

        # --- Finalise ---
        finish_event = threading.Event()
        writer.finishWritingWithCompletionHandler_(lambda: finish_event.set())
        finish_event.wait()

        if writer.status() != AVFoundation.AVAssetWriterStatusCompleted:
            err = writer.error()
            mode = "audio-fix passthrough" if is_passthrough_video else "HEVC re-encode"
            raise RuntimeError(
                f"{mode} failed for {src}: "
                f"{err.localizedDescription() if err else 'unknown error'}"
            )
        if reader.status() == AVFoundation.AVAssetReaderStatusFailed:
            err = reader.error()
            mode = "audio-fix passthrough" if is_passthrough_video else "HEVC re-encode"
            raise RuntimeError(
                f"{mode} reader failed for {src}: "
                f"{err.localizedDescription() if err else 'unknown error'}"
            )


def reencode_to_hevc(
    source_path: str | os.PathLike,
    dest_path: str | os.PathLike,
    *,
    bitrate: int = 10_000_000,
) -> None:
    """Re-encode `source_path` to HEVC at `bitrate` bits/s.

    HDR colour tags (HLG, HDR10/PQ) are propagated from the source. For
    sources tagged HDR, the HEVC Main10 profile is requested so the encoder
    doesn't quietly drop to 8-bit. Audio is normalised to AAC unless the
    source is already AAC (then it passes through).
    """
    _av_convert(
        source_path,
        dest_path,
        video_codec=AVFoundation.AVVideoCodecTypeHEVC,
        video_bitrate=bitrate,
    )


def normalize_audio_to_aac(
    source_path: str | os.PathLike,
    dest_path: str | os.PathLike,
) -> None:
    """Pass the video stream through byte-perfect; re-encode audio to AAC.

    Used by the GUI's non-compress path when the source has non-AAC audio
    (e.g. PCM from a Sony XAVC clip). Photos rejects Live Photos whose
    paired video carries non-AAC audio; this turns such files into
    importable assets without touching the video pixels.

    The output is a QuickTime `.mov` container with:
      - the original video track's encoded samples carried over verbatim
        (sourceFormatHint passthrough),
      - the original audio decoded to PCM and re-encoded as AAC 192 kbps.
    """
    _av_convert(
        source_path,
        dest_path,
        video_codec=None,
        video_bitrate=None,
    )


# ---------- Live Photo compatibility pre-flight ----------


# Codecs Apple Photos accepts in the video track of a Live Photo paired
# resource. Anything else is silently rejected by PhotoKit with the generic
# "operation could not be completed" message, so we name it up-front.
_LIVE_PHOTO_VIDEO_CODECS = {
    AVFoundation.AVVideoCodecTypeH264,
    AVFoundation.AVVideoCodecTypeHEVC,
}


def check_live_photo_video(video_path: str | os.PathLike) -> list[str]:
    """Inspect a video for known Live-Photo incompatibilities.

    Returns a list of human-readable issues. An empty list means the video
    should be accepted by PhotoKit as a Live-Photo paired resource.

    The checks are cheap — no decode — and use only AVAsset metadata.
    """
    src = pathlib.Path(video_path)
    issues: list[str] = []

    with objc.autorelease_pool():
        asset = AVFoundation.AVAsset.assetWithURL_(NSURL.fileURLWithPath_(str(src)))
        if asset is None:
            issues.append(f"AVFoundation can't open '{src.name}'")
            return issues

        # Duration: Apple's own Live Photos are ~3s. Photos has been observed
        # to accept videos up to ~30s; longer than that and the import tends
        # to silently fail. We warn (don't reject) so the user knows.
        duration_cm = asset.duration()
        if duration_cm.timescale:
            duration = float(duration_cm.value) / float(duration_cm.timescale)
            if duration > 60.0:
                issues.append(
                    f"video duration is {duration:.1f}s — Photos may reject very long Live Photos"
                )

        # Video codec.
        video_tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
        if not video_tracks:
            issues.append("no video track")
        else:
            descs = video_tracks[0].formatDescriptions()
            if descs:
                subtype = CoreMedia.CMFormatDescriptionGetMediaSubType(descs[0])
                fourcc = _fourcc_to_str(subtype)
                if fourcc not in _LIVE_PHOTO_VIDEO_CODECS:
                    issues.append(
                        f"video codec is '{fourcc}' — Live Photos need H.264 or HEVC. "
                        f"Enable 'Compress video to HEVC' to auto-fix."
                    )

        # Audio codec (if any).
        audio_tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeAudio)
        if audio_tracks and not _is_aac_audio(audio_tracks[0]):
            subtype = _audio_subtype(audio_tracks[0])
            fourcc = _fourcc_to_str(subtype) if subtype else "?"
            issues.append(
                f"audio codec is '{fourcc}' — Live Photos need AAC. "
                f"Enable 'Compress video to HEVC' to auto-fix."
            )

    return issues
