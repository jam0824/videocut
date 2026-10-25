"""無劣化カットの統合テスト（実際に動画を書き出して中身を検査する）"""

import pytest

from media_factory import beep_times, decode_frames, frame_indices, make_indexed_video, stream_types
from videocut.copycut import Cancelled, copy_cut
from videocut.plan import OutputSpec, temp_path_for
from videocut.ranges import Range


def cut(src, ranges, out):
    copy_cut(str(src), [OutputSpec(str(out), tuple(ranges))])
    return out


def assert_in_sync(path, audio_index=0, fps=60):
    """ビープ（元動画の毎秒ちょうど）の位置に、元動画の毎秒ちょうどのフレームがある"""
    frames = decode_frames(path)
    beeps = beep_times(path, audio_index)
    assert beeps, "ビープが見つからない"
    for beep in beeps:
        time, index = min(frames, key=lambda f: abs(f[0] - beep))
        assert abs(time - beep) < 0.004, f"ビープ {beep:.4f}s の近くにフレームが無い"
        assert index % fps == 0, f"ビープ {beep:.4f}s の位置のフレームが {index}（音ズレ）"


class TestCopyCut:
    """音声の検査では、つなぎ目をビープ（毎秒ちょうど）から離して置く。
    AAC はパケットが重なり合って復号されるため、つなぎ目の前後約21msには隣の音が混ざる"""

    def test_キーフレーム上の区間をそのまま抜き出す(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(2.0, 4.0)], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(120, 240))
        assert_in_sync(out)

    def test_出力の先頭の音声が欠けない(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(1.0, 2.5)], tmp_path / "out.mp4")
        assert beep_times(out) == pytest.approx([0.0, 1.0], abs=0.002)

    def test_複数の区間を順につなげる(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(0.5, 1.5), Range(4.5, 6.5)], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(30, 90)) + list(range(270, 390))
        assert beep_times(out) == pytest.approx([0.5, 1.5, 2.5], abs=0.003)
        assert_in_sync(out)

    def test_つなぎ目でも映像の時刻が等間隔に続く(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(1.0, 2.0), Range(5.0, 6.5)], tmp_path / "out.mp4")
        times = [t for t, _ in decode_frames(out)]
        assert times == pytest.approx([i / 60 for i in range(len(times))], abs=1e-3)

    def test_末尾までの区間は最後のフレームまで残す(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(0, 3.5), Range(6.5, 10.0)], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(0, 210)) + list(range(390, 600))
        assert beep_times(out) == pytest.approx([0, 1, 2, 3, 4, 5, 6], abs=0.003)
        assert_in_sync(out)

    def test_キーフレームでない位置は外側のキーフレームまで広げて残す(self, tmp_path):
        src = make_indexed_video(tmp_path / "longgop.mp4", seconds=20, gop=250)
        out = cut(src, [Range(5.0, 12.0)], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(250, 750))
        assert_in_sync(out)

    def test_音声トラックをすべて残して音ズレしない(self, offset_mkv, tmp_path):
        out = cut(offset_mkv, [Range(2.5, 3.5), Range(5.5, 8.0)], tmp_path / "out.mkv")
        assert stream_types(out) == ["video", "audio", "audio"]
        assert frame_indices(out) == list(range(150, 210)) + list(range(330, 480))
        assert_in_sync(out, audio_index=0)
        assert_in_sync(out, audio_index=1)

    def test_映像が遅れて始まる動画でも映像と音声をそろえて切る(self, delayed_video_mp4, tmp_path):
        out = cut(delayed_video_mp4, [Range(0.25 + 1.5, 0.25 + 3.5)], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(90, 210))
        assert_in_sync(out)

    def test_出力ごとに先頭から始まる別ファイルにできる(self, indexed_mp4, tmp_path):
        outputs = [
            OutputSpec(str(tmp_path / "a.mp4"), (Range(1.0, 2.0),)),
            OutputSpec(str(tmp_path / "b.mp4"), (Range(4.0, 5.0),)),
        ]
        copy_cut(str(indexed_mp4), outputs)
        assert frame_indices(tmp_path / "a.mp4") == list(range(60, 120))
        assert frame_indices(tmp_path / "b.mp4") == list(range(240, 300))
        assert decode_frames(tmp_path / "b.mp4")[0][0] == pytest.approx(0.0, abs=1e-3)

    def test_日本語を含むフォルダに書き出せる(self, indexed_mp4, tmp_path):
        folder = tmp_path / "出力 フォルダ"
        folder.mkdir()
        out = cut(indexed_mp4, [Range(1.0, 2.0)], folder / "切り抜き.mp4")
        assert frame_indices(out) == list(range(60, 120))

    def test_進捗を0から1まで通知する(self, indexed_mp4, tmp_path):
        values = []
        copy_cut(
            str(indexed_mp4),
            [OutputSpec(str(tmp_path / "out.mp4"), (Range(0, 4.0), Range(6.0, 9.0)))],
            on_progress=values.append,
        )
        assert values[-1] == 1.0
        assert values == sorted(values)

    def test_中止すると出力ファイルも一時ファイルも残さない(self, indexed_mp4, tmp_path):
        out = tmp_path / "out.mp4"
        with pytest.raises(Cancelled):
            copy_cut(str(indexed_mp4), [OutputSpec(str(out), (Range(0, 10.0),))], should_cancel=lambda: True)
        assert not out.exists()
        assert not (tmp_path / "out.videocut-tmp.mp4").exists()
        assert temp_path_for(str(out)).endswith("out.videocut-tmp.mp4")
