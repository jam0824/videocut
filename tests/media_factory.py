"""テスト用の動画を作り、書き出し結果を検査するヘルパー

映像の各フレームにはフレーム番号を白黒ブロック（16bit）で描き、
音声トラックには映像の開始から毎秒ちょうどに短いビープを入れる。
書き出し後にフレーム番号とビープ位置を読み取れば、どのフレームが残ったかと
音ズレの有無を検証できる。
"""

from fractions import Fraction
from pathlib import Path

import av
import numpy as np

WIDTH, HEIGHT = 320, 180
_BITS = 16
_BLOCK = 20
SAMPLE_RATE = 48000


def _frame_image(index: int) -> np.ndarray:
    img = np.full((HEIGHT, WIDTH), 128, dtype=np.uint8)
    for b in range(_BITS):
        x0 = b * _BLOCK
        bit = (index >> b) & 1
        img[20:40, x0 : x0 + _BLOCK] = 235 if bit else 16
        img[60:80, x0 : x0 + _BLOCK] = 16 if bit else 235  # 反転パターン（読み取り誤りの検出用）
    return img


def _read_index(gray: np.ndarray) -> int:
    value = 0
    for b in range(_BITS):
        cx = b * _BLOCK + _BLOCK // 2
        top = gray[30, cx] > 128
        if top == (gray[70, cx] > 128):
            return -1
        if top:
            value |= 1 << b
    return value


def make_indexed_video(
    path: Path,
    seconds: float = 10.0,
    fps: int = 60,
    gop: int = 30,
    bframes: int = 3,
    audio_tracks: int = 1,
    start_offset: float = 0.0,
    video_delay: float = 0.0,
) -> Path:
    """start_offset: 全ストリームの開始時刻 / video_delay: 映像だけ音声より遅れて始まる秒数"""
    frames = round(seconds * fps)
    with av.open(str(path), "w") as out:
        video = out.add_stream("libx264", rate=fps)
        video.width, video.height, video.pix_fmt = WIDTH, HEIGHT, "yuv420p"
        video.options = {
            "crf": "18",
            "g": str(gop),
            "keyint_min": str(gop),
            "sc_threshold": "0",
            "bf": str(bframes),
        }
        audios = [out.add_stream("aac", rate=SAMPLE_RATE, layout="stereo") for _ in range(audio_tracks)]

        video_offset = round((start_offset + video_delay) * fps)
        for i in range(frames):
            frame = av.VideoFrame.from_ndarray(_frame_image(i), format="gray").reformat(format="yuv420p")
            frame.pts = video_offset + i
            frame.time_base = Fraction(1, fps)
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode():
            out.mux(packet)

        total = round((seconds + video_delay) * SAMPLE_RATE)
        audio_offset = round(start_offset * SAMPLE_RATE)
        for track, stream in enumerate(audios):
            samples = np.zeros(total, dtype=np.float32)
            t = np.arange(480) / SAMPLE_RATE
            beep = 0.5 * np.sin(2 * np.pi * 1000 * (track + 1) * t).astype(np.float32)
            for second in range(int(seconds)):
                start = round((second + video_delay) * SAMPLE_RATE)
                samples[start : start + 480] = beep[: max(0, min(480, total - start))]
            for start in range(0, total, 1024):
                block = samples[start : start + 1024]
                frame = av.AudioFrame.from_ndarray(np.vstack([block, block]), format="fltp", layout="stereo")
                frame.sample_rate = SAMPLE_RATE
                frame.pts = audio_offset + start
                frame.time_base = Fraction(1, SAMPLE_RATE)
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
    return path


def decode_frames(path: Path) -> list[tuple[float, int]]:
    """(コンテナ先頭からの秒, フレーム番号) を表示順に返す。読めないフレームの番号は -1"""
    with av.open(str(path)) as c:
        start = (c.start_time or 0) / 1e6
        return [(frame.time - start, _read_index(frame.to_ndarray(format="gray"))) for frame in c.decode(video=0)]


def frame_indices(path: Path) -> list[int]:
    return [index for _, index in decode_frames(path)]


def beep_times(path: Path, audio_index: int = 0) -> list[float]:
    """音声トラックのビープの立ち上がり時刻（コンテナ先頭からの秒）"""
    times: list[float] = []
    last_loud = None
    with av.open(str(path)) as c:
        start = (c.start_time or 0) / 1e6
        for frame in c.decode(audio=audio_index):
            t0 = frame.time - start
            for i in np.flatnonzero(np.abs(frame.to_ndarray()[0]) > 0.2):
                t = t0 + i / frame.sample_rate
                if last_loud is None or t - last_loud > 0.1:
                    times.append(t)
                last_loud = t
    return times


def frame_shown_at(frames: list[tuple[float, int]], t: float) -> int:
    """時刻 t に表示されているフレームの番号"""
    shown = frames[0][1]
    for time, index in frames:
        if time > t + 1e-4:
            break
        shown = index
    return shown


def stream_types(path: Path) -> list[str]:
    with av.open(str(path)) as c:
        return [s.type for s in c.streams]
