import bisect

import pytest

from videocut.ranges import (
    Mode,
    Range,
    complement,
    keep_ranges,
    normalize,
    snap_inward,
    snap_outward,
    total_length,
)


class KeyframeList:
    """テスト用: 固定のキーフレーム時刻の一覧から検索する"""

    def __init__(self, times):
        self.times = sorted(times)

    def at_or_before(self, t):
        i = bisect.bisect_right(self.times, t)
        return self.times[i - 1] if i > 0 else None

    def after(self, t):
        i = bisect.bisect_right(self.times, t)
        return self.times[i] if i < len(self.times) else None


class TestRange:
    def test_長さを返す(self):
        assert Range(1.5, 4.0).length == pytest.approx(2.5)

    def test_開始が終了以降ならValueError(self):
        with pytest.raises(ValueError):
            Range(5.0, 5.0)


class TestNormalize:
    def test_重なる区間を結合する(self):
        assert normalize([Range(0, 5), Range(3, 8)]) == [Range(0, 8)]

    def test_接する区間を結合する(self):
        assert normalize([Range(0, 5), Range(5, 8)]) == [Range(0, 8)]

    def test_開始時刻の順に並べる(self):
        assert normalize([Range(6, 8), Range(1, 2)]) == [Range(1, 2), Range(6, 8)]

    def test_動画の長さの範囲に切り詰める(self):
        assert normalize([Range(-1, 2), Range(8, 12)], duration=10) == [Range(0, 2), Range(8, 10)]

    def test_最小の長さ未満の区間は捨てる(self):
        assert normalize([Range(1, 1.005), Range(3, 4)], min_length=0.01) == [Range(3, 4)]

    def test_動画の範囲外の区間は捨てる(self):
        assert normalize([Range(11, 12)], duration=10) == []


class TestComplement:
    def test_指定区間を除いた残りを返す(self):
        result = complement([Range(2, 4), Range(6, 7)], duration=10)
        assert result == [Range(0, 2), Range(4, 6), Range(7, 10)]

    def test_先頭からの区間を除く(self):
        assert complement([Range(0, 3)], duration=10) == [Range(3, 10)]

    def test_末尾までの区間を除く(self):
        assert complement([Range(7, 10)], duration=10) == [Range(0, 7)]

    def test_全体を除くと空になる(self):
        assert complement([Range(0, 10)], duration=10) == []

    def test_区間が無ければ全体を返す(self):
        assert complement([], duration=10) == [Range(0, 10)]


class TestKeepRanges:
    def test_抜き出しは選んだ区間を残す(self):
        selected = [Range(5, 6), Range(1, 2)]
        assert keep_ranges(Mode.EXTRACT, selected, duration=10) == [Range(1, 2), Range(5, 6)]

    def test_削除は選んだ区間以外を残す(self):
        selected = [Range(1, 2), Range(5, 6)]
        assert keep_ranges(Mode.DELETE, selected, duration=10) == [Range(0, 1), Range(2, 5), Range(6, 10)]

    def test_残る長さの合計を返す(self):
        assert total_length([Range(0, 1), Range(2, 5)]) == pytest.approx(4.0)


class TestSnapOutward:
    """無劣化カットで残す区間をキーフレームの外側へ広げる"""

    keyframes = KeyframeList([0, 2, 4, 6, 8])

    def test_開始は直前のキーフレームまで広げる(self):
        assert snap_outward([Range(3, 4)], self.keyframes, duration=9.5) == [Range(2, 4)]

    def test_終了は直後のキーフレームまで広げる(self):
        assert snap_outward([Range(2, 5)], self.keyframes, duration=9.5) == [Range(2, 6)]

    def test_キーフレーム上の境界はそのまま使う(self):
        assert snap_outward([Range(2, 4)], self.keyframes, duration=9.5) == [Range(2, 4)]

    def test_1ms未満のずれはキーフレーム上とみなす(self):
        assert snap_outward([Range(2.0004, 3.9996)], self.keyframes, duration=9.5) == [Range(2, 4)]

    def test_最後のキーフレームより後の終了は動画の末尾まで広げる(self):
        assert snap_outward([Range(8.5, 9)], self.keyframes, duration=9.5) == [Range(8, 9.5)]

    def test_広げて重なった区間は結合する(self):
        assert snap_outward([Range(1, 2.5), Range(3, 3.5)], self.keyframes, duration=9.5) == [Range(0, 4)]

    def test_最初のキーフレームより前の開始は0にする(self):
        keyframes = KeyframeList([0.5, 2.5, 4.5])
        assert snap_outward([Range(0.2, 1)], keyframes, duration=5) == [Range(0, 2.5)]


class TestSnapInward:
    """無劣化カットで実際に削除される範囲（キーフレームの内側）を求める"""

    keyframes = KeyframeList([0, 2, 4, 6, 8])

    def test_削除区間の内側のキーフレームまで縮める(self):
        assert snap_inward(Range(1, 7), self.keyframes, duration=9.5) == Range(2, 6)

    def test_キーフレーム間隔より短いと削除されない(self):
        assert snap_inward(Range(2.5, 3.5), self.keyframes, duration=9.5) is None

    def test_末尾までの削除は末尾まで消す(self):
        assert snap_inward(Range(7, 9.5), self.keyframes, duration=9.5) == Range(8, 9.5)

    def test_先頭からの削除は先頭から消す(self):
        assert snap_inward(Range(0, 3), self.keyframes, duration=9.5) == Range(0, 2)
