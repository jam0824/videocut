"""正確なカット: 区間の映像・音声をデコードし直して再エンコードする（フレーム単位）

指定したフレームちょうどで切れるが、エンコードの分だけ時間がかかり、画質もわずかに落ちる。
GPU エンコーダー（NVENC など）が使えればそれを使う。
"""

import math
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from fractions import Fraction

import av
import numpy as np
from av.video.frame import PictureType

from videocut.exporting import CancelFn, Progress, ProgressFn, atomic_output, check_cancel, copy_stream_metadata
from videocut.media import AudioInfo, MediaInfo, open_media, probe
from videocut.plan import OutputSpec, reencode_audio_bitrate, reencode_video_bitrate
from videocut.ranges import Range, total_length

EncoderCandidates = Sequence[tuple[str, dict[str, str]]]

# 元の映像コーデックごとのエンコーダー候補。先頭から順に試し、最初に使えたものを使う
VIDEO_ENCODERS: dict[str, list[tuple[str, dict[str, str]]]] = {
    "h264": [
        ("h264_nvenc", {"preset": "p4", "rc": "vbr"}),
        ("h264_qsv", {"preset": "medium"}),
        ("h264_amf", {"quality": "balanced"}),
        ("libx264", {"preset": "veryfast"}),
    ],
    "hevc": [
        ("hevc_nvenc", {"preset": "p4", "rc": "vbr"}),
        ("hevc_qsv", {"preset": "medium"}),
        ("hevc_amf", {"quality": "balanced"}),
        ("libx265", {"preset": "fast"}),
    ],
    "av1": [
        ("av1_nvenc", {"preset": "p4", "rc": "vbr"}),
        ("av1_qsv", {"preset": "medium"}),
        ("av1_amf", {"quality": "balanced"}),
        ("libsvtav1", {"preset": "8"}),
    ],
}

_READ_PAST_END = 3.0
_CANCEL_CHECK_PACKETS = 100


class ReencodeError(Exception):
    pass


@dataclass(frozen=True)
class EncoderChoice:
    name: str
    options: dict[str, str] = field(default_factory=dict)
    pix_fmt: str = "yuv420p"


def default_candidates(codec: str) -> list[tuple[str, dict[str, str]]]:
    return VIDEO_ENCODERS.get(codec, VIDEO_ENCODERS["h264"])


def choose_video_encoder(width: int, height: int, src_pix_fmt: str | None, candidates: EncoderCandidates) -> EncoderChoice:
    """候補を順に実際に開いてみて、最初に使えたエンコーダーを返す"""
    errors = []
    for name, options in candidates:
        try:
            pix_fmt = _pick_pix_fmt(name, src_pix_fmt)
            _try_encoder(name, options, width, height, pix_fmt)
            return EncoderChoice(name, dict(options), pix_fmt)
        except Exception as e:  # noqa: BLE001 - GPU が無い等、理由を問わず次の候補へ
            errors.append(f"{name}: {e}")
    raise ReencodeError("使える映像エンコーダーが見つかりません\n" + "\n".join(errors))


def _pick_pix_fmt(encoder: str, src_pix_fmt: str | None) -> str:
    formats = [f.name for f in (av.Codec(encoder, "w").video_formats or [])]
    if not formats or src_pix_fmt in formats:
        return src_pix_fmt or "yuv420p"
    ten_bit = bool(src_pix_fmt) and ("10" in src_pix_fmt or "p010" in src_pix_fmt)
    for name in (["p010le", "yuv420p10le"] if ten_bit else []) + ["yuv420p", "nv12"]:
        if name in formats:
            return name
    return formats[0]


def _try_encoder(name: str, options: dict[str, str], width: int, height: int, pix_fmt: str) -> None:
    ctx = av.CodecContext.create(name, "w")
    ctx.width, ctx.height, ctx.pix_fmt = width, height, pix_fmt
    ctx.time_base = Fraction(1, 60)
    ctx.framerate = Fraction(60)
    ctx.bit_rate = 2_000_000
    ctx.options = dict(options)
    frame = av.VideoFrame(width, height, pix_fmt)
    frame.pts = 0
    ctx.encode(frame)
    ctx.encode(None)


def reencode_cut(
    source: str,
    outputs: Sequence[OutputSpec],
    on_progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    video_encoders: EncoderCandidates | None = None,
) -> None:
    info = probe(source)
    choice = choose_video_encoder(
        info.video.width,
        info.video.height,
        info.video.pix_fmt,
        video_encoders or default_candidates(info.video.codec),
    )
    progress = Progress(on_progress, sum(total_length(spec.ranges) for spec in outputs) * float(info.video.fps))
    frames_done = 0
    for spec in outputs:
        with atomic_output(spec.path) as tmp:
            with open_media(source) as inp, av.open(tmp, "w") as out:
                transcoder = _Transcoder(inp, out, info, choice, progress, should_cancel, frames_done)
                transcoder.run(spec.ranges)
                frames_done = transcoder.frames_done
    progress.finish()


class _Transcoder:
    """1つの出力ファイルへ、指定区間を順に再エンコードしてつなげる"""

    def __init__(self, inp, out, info: MediaInfo, choice: EncoderChoice, progress: Progress, should_cancel, frames_done):
        self.inp, self.out, self.info = inp, out, info
        self.progress, self.should_cancel = progress, should_cancel
        self.frames_done = frames_done
        self.video_in = inp.streams[info.video.index]
        self.video_in.codec_context.thread_type = "AUTO"
        self.audio_in = [inp.streams[a.index] for a in info.audio]
        self.video_out = self._add_video(choice)
        self.audio_out = {s.index: self._add_audio(s, a) for s, a in zip(self.audio_in, info.audio, strict=True)}
        self.samples_written = {s.index: 0 for s in self.audio_in}
        self.last_video_pts: int | None = None

    def _add_video(self, choice: EncoderChoice):
        src, info = self.video_in, self.info.video
        stream = self.out.add_stream(choice.name, rate=info.fps)
        stream.width, stream.height, stream.pix_fmt = info.width, info.height, choice.pix_fmt
        stream.time_base = src.time_base
        bit_rate = reencode_video_bitrate(info.bit_rate)
        stream.bit_rate = bit_rate
        stream.options = {**choice.options, "maxrate": str(bit_rate * 2), "bufsize": str(bit_rate * 2)}
        for attr in ("color_range", "colorspace", "color_primaries", "color_trc"):
            with suppress(AttributeError, TypeError, ValueError):
                setattr(stream.codec_context, attr, getattr(src.codec_context, attr))
        copy_stream_metadata(src, stream)
        return stream

    def _add_audio(self, src, info: AudioInfo):
        stream = self.out.add_stream("aac", rate=src.sample_rate, layout=src.layout.name)
        stream.bit_rate = reencode_audio_bitrate(info.bit_rate)
        copy_stream_metadata(src, stream)
        return stream

    def run(self, ranges: Sequence[Range]) -> None:
        offset = Fraction(0)
        for r in ranges:
            self._transcode_range(r, offset)
            offset += Fraction(r.end) - Fraction(r.start)
        for packet in self.video_out.encode(None):
            self.out.mux(packet)
        for stream in self.audio_out.values():
            for packet in stream.encode(None):
                self.out.mux(packet)

    def _transcode_range(self, r: Range, offset: Fraction) -> None:
        start = self.info.start_time
        # 1秒手前から読む: キーフレームから復号し直す必要があり、AAC も直前のデータが無いと頭が欠けるため
        self.inp.seek(
            int((max(r.start - 1.0, 0.0) + start) / self.video_in.time_base),
            stream=self.video_in,
            backward=True,
            any_frame=False,
        )
        for stream in (self.video_in, *self.audio_in):
            stream.codec_context.flush_buffers()

        video_done = False
        audio_done: set[int] = set()
        for count, packet in enumerate(self.inp.demux([self.video_in, *self.audio_in]), start=1):
            stream = packet.stream
            for frame in packet.decode():
                if stream.index == self.video_in.index:
                    video_done |= self._video_frame(frame, r, offset)
                elif self._audio_frame(stream.index, frame, r, offset):
                    audio_done.add(stream.index)
            if video_done and len(audio_done) == len(self.audio_in):
                break
            if video_done and packet.pts is not None and float(packet.pts * stream.time_base) - start > r.end + _READ_PAST_END:
                break
            if count % _CANCEL_CHECK_PACKETS == 0:
                check_cancel(self.should_cancel)

    def _video_frame(self, frame, r: Range, offset: Fraction) -> bool:
        """区間内のフレームをエンコードする。区間の終わりに達したら True"""
        if frame.pts is None:
            return False
        t = frame.time - self.info.start_time
        half = self.info.video.frame_duration / 2
        if t >= r.end - half:
            return True
        if t < r.start - half:
            return False
        tb = self.video_out.time_base
        pts = frame.pts + round((offset - Fraction(r.start) - Fraction(self.info.start_time)) / tb)
        if self.last_video_pts is not None and pts <= self.last_video_pts:
            pts = self.last_video_pts + 1
        self.last_video_pts = pts
        frame.pts = pts
        frame.time_base = tb
        frame.pict_type = PictureType.NONE  # 元のIフレーム位置を引き継がず、エンコーダーに任せる
        for packet in self.video_out.encode(frame):
            self.out.mux(packet)
        self.frames_done += 1
        self.progress.update(self.frames_done)
        return False

    def _audio_frame(self, index: int, frame, r: Range, offset: Fraction) -> bool:
        """区間内のサンプルだけを切り出してエンコードする。区間の終わりに達したら True"""
        if frame.pts is None:
            return False
        rate = frame.sample_rate
        t = frame.time - self.info.start_time
        if t >= r.end:
            return True
        n = frame.samples
        first = max(0, math.ceil((r.start - t) * rate - 1e-6))
        last = min(n, math.ceil((r.end - t) * rate - 1e-6))
        if last <= first:
            return False

        # 出力上でこのサンプルが来るべき位置に合わせる（元の音声に欠けや重なりがあってもズレをためない）
        expected = round((t + first / rate - r.start + float(offset)) * rate)
        written = self.samples_written[index]
        tolerance = rate // 1000
        if expected > written + tolerance:
            self._encode_audio(index, _silence_like(frame, expected - written))
        elif expected < written - tolerance:
            first = min(last, first + written - expected)
        if last <= first:
            return last < n

        part = frame if (first, last) == (0, n) else _slice_audio(frame, first, last)
        self._encode_audio(index, part)
        return last < n

    def _encode_audio(self, index: int, frame) -> None:
        frame.pts = self.samples_written[index]
        frame.time_base = Fraction(1, frame.sample_rate)
        self.samples_written[index] += frame.samples
        for packet in self.audio_out[index].encode(frame):
            self.out.mux(packet)


def _slice_audio(frame, first: int, last: int):
    data = frame.to_ndarray()
    if frame.format.is_planar:
        part = data[:, first:last]
    else:
        channels = len(frame.layout.channels)
        part = data[:, first * channels : last * channels]
    return _audio_frame_from(np.ascontiguousarray(part), frame)


def _silence_like(frame, samples: int):
    data = frame.to_ndarray()
    width = samples if frame.format.is_planar else samples * len(frame.layout.channels)
    return _audio_frame_from(np.zeros((data.shape[0], width), dtype=data.dtype), frame)


def _audio_frame_from(data: np.ndarray, like):
    frame = av.AudioFrame.from_ndarray(data, format=like.format.name, layout=like.layout.name)
    frame.sample_rate = like.sample_rate
    return frame
