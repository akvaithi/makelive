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


def reencode_to_hevc(
    source_path: str | os.PathLike,
    dest_path: str | os.PathLike,
    *,
    bitrate: int = 10_000_000,
) -> None:
    """Re-encode `source_path` to HEVC at `bitrate` bits/s.

    HDR colour tags (HLG, HDR10/PQ) are propagated from the source. For
    sources tagged HDR, the HEVC Main10 profile is requested so the encoder
    doesn't quietly drop to 8-bit. Audio passes through (copied as the
    source codec — no transcode).
    """
    src = pathlib.Path(source_path)
    dst = pathlib.Path(dest_path)
    if dst.exists():
        dst.unlink()

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

        color_props = _extract_color_properties(video_track)
        hdr = color_props is not None and _is_hdr_transfer(
            color_props.get(AVFoundation.AVVideoTransferFunctionKey)
        )

        # --- Reader ---
        reader, err = AVFoundation.AVAssetReader.alloc().initWithAsset_error_(asset, None)
        if reader is None:
            raise RuntimeError(f"AVAssetReader init failed: {err}")

        pixel_format = _pixel_format_for_source(video_track, hdr)
        video_reader_output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
            video_track,
            {Quartz.kCVPixelBufferPixelFormatTypeKey: pixel_format},
        )
        video_reader_output.setAlwaysCopiesSampleData_(False)
        reader.addOutput_(video_reader_output)

        audio_reader_output = None
        if audio_tracks:
            # outputSettings=None ⇒ delivers samples in the source codec (passthrough).
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

        compression: dict = {AVFoundation.AVVideoAverageBitRateKey: int(bitrate)}
        if hdr:
            profile_key = getattr(
                AVFoundation, "kVTProfileLevel_HEVC_Main10_AutoLevel", None
            )
            if profile_key is not None:
                compression[AVFoundation.AVVideoProfileLevelKey] = profile_key

        video_writer_settings: dict = {
            AVFoundation.AVVideoCodecKey: AVFoundation.AVVideoCodecTypeHEVC,
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
            # outputSettings=None ⇒ passthrough using the source's audio format.
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
            raise RuntimeError(
                f"HEVC re-encode failed for {src}: "
                f"{err.localizedDescription() if err else 'unknown error'}"
            )
        if reader.status() == AVFoundation.AVAssetReaderStatusFailed:
            err = reader.error()
            raise RuntimeError(
                f"HEVC re-encode reader failed for {src}: "
                f"{err.localizedDescription() if err else 'unknown error'}"
            )
