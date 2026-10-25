"""区間（秒）の計算: 結合・反転・キーフレームへの合わせ込み

時刻はすべて「コンテナの先頭（start_time）からの秒」で扱う。
"""

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

# キーフレーム時刻とみなす誤差。フレーム間隔（60fpsで約16.7ms）より十分小さい値
SNAP_TOLERANCE = 0.001


@dataclass(frozen=True)
class Range:
    start: float
    end: float

    def __post_init__(self) -> None:
        if not self.end > self.start:
            raise ValueError(f"区間の終了が開始以前です: {self.start} - {self.end}")

    @property
    def length(self) -> float:
        return self.end - self.start


class Mode(enum.Enum):
    EXTRACT = "extract"  # 選んだ区間を残す
    DELETE = "delete"  # 選んだ区間を消す


class KeyframeLookup(Protocol):
    def at_or_before(self, t: float) -> float | None:
        """t 以下で最も遅いキーフレームの時刻"""
        ...

    def after(self, t: float) -> float | None:
        """t より後で最も早いキーフレームの時刻"""
        ...


def normalize(ranges: Iterable[Range], duration: float | None = None, min_length: float = 1e-6) -> list[Range]:
    """動画の範囲に切り詰め、短すぎる区間を捨て、重なる・接する区間を結合する"""
    merged: list[Range] = []
    for r in sorted(ranges, key=lambda r: r.start):
        start = max(0.0, r.start)
        end = r.end if duration is None else min(duration, r.end)
        if end - start < min_length:
            continue
        if merged and start <= merged[-1].end:
            if end > merged[-1].end:
                merged[-1] = Range(merged[-1].start, end)
        else:
            merged.append(Range(start, end))
    return merged


def complement(ranges: Iterable[Range], duration: float, min_length: float = 1e-6) -> list[Range]:
    """[0, duration] から指定区間を除いた残り"""
    result: list[Range] = []
    cursor = 0.0
    for r in normalize(ranges, duration, min_length):
        if r.start - cursor >= min_length:
            result.append(Range(cursor, r.start))
        cursor = max(cursor, r.end)
    if duration - cursor >= min_length:
        result.append(Range(cursor, duration))
    return result


def keep_ranges(mode: Mode, selected: Iterable[Range], duration: float, min_length: float = 1e-6) -> list[Range]:
    """書き出しで残す区間"""
    if mode is Mode.EXTRACT:
        return normalize(selected, duration, min_length)
    return complement(selected, duration, min_length)


def total_length(ranges: Iterable[Range]) -> float:
    return sum(r.length for r in ranges)


def snap_outward(
    ranges: Iterable[Range], keyframes: KeyframeLookup, duration: float, tol: float = SNAP_TOLERANCE
) -> list[Range]:
    """残す区間をキーフレームの外側へ広げる（無劣化カットで残す範囲）

    指定した部分が欠けないよう、開始は直前・終了は直後のキーフレームに合わせる。
    """
    snapped = []
    for r in ranges:
        start = keyframes.at_or_before(r.start + tol)
        end = duration if r.end >= duration - tol else keyframes.after(r.end - tol)
        snapped.append(Range(0.0 if start is None else start, duration if end is None else end))
    return normalize(snapped, duration)


def snap_inward(r: Range, keyframes: KeyframeLookup, duration: float, tol: float = SNAP_TOLERANCE) -> Range | None:
    """削除区間のうち無劣化カットで実際に消える範囲。消えない場合は None"""
    start = 0.0 if r.start <= tol else keyframes.after(r.start - tol)
    end = duration if r.end >= duration - tol else keyframes.at_or_before(r.end + tol)
    if start is None or end is None or end - start <= tol:
        return None
    return Range(start, end)
