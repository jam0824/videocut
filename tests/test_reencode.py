"""正確なカット（再エンコード）の統合テスト"""

import av
import pytest

from media_factory import beep_times, decode_frames, frame_indices, stream_types
from test_copycut import assert_in_sync
from videocut.plan import OutputSpec
from videocut.reencode import ReencodeError, choose_video_encoder, reencode_cut
from videocut.ranges import Range

X264 = [("libx264", {"preset": "ultrafast"})]


def cut(src, ranges, out, encoders=X264):
    reencode_cut(str(src), [OutputSpec(str(out), tuple(ranges))], video_encoders=encoders)
    return out


def frame(n):
    return n / 60


class TestReencodeCut:
    def test_指定したフレームちょうどで切る(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(frame(126), frame(234))], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(126, 234))

    def test_複数の区間をフレーム単位でつなげて音ズレしない(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(frame(65), frame(120)), Range(frame(330), frame(375))], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(65, 120)) + list(range(330, 375))
        # 元の 6.0 秒（2つ目の区間の 0.5 秒目）のビープ
        assert beep_times(out) == pytest.approx([frame(55) + 0.5], abs=0.002)
        assert_in_sync(out)

    def test_つなぎ目でも映像の時刻が等間隔に続く(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(frame(65), frame(120)), Range(frame(330), frame(375))], tmp_path / "out.mp4")
        times = [t for t, _ in decode_frames(out)]
        assert times == pytest.approx([i / 60 for i in range(len(times))], abs=1e-3)

    def test_映像と音声の長さがそろう(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(1.0, 2.5), Range(6.0, 7.0)], tmp_path / "out.mp4")
        with av.open(str(out)) as c:
            video = float(c.streams.video[0].duration * c.streams.video[0].time_base)
            audio = float(c.streams.audio[0].duration * c.streams.audio[0].time_base)
        assert video == pytest.approx(2.5, abs=0.02)
        assert audio == pytest.approx(2.5, abs=0.03)

    def test_元と同じ解像度とコーデックで書き出す(self, indexed_mp4, tmp_path):
        out = cut(indexed_mp4, [Range(1.0, 2.0)], tmp_path / "out.mp4")
        with av.open(str(out)) as c:
            v = c.streams.video[0]
            assert (v.codec_context.name, v.width, v.height) == ("h264", 320, 180)
            assert c.streams.audio[0].codec_context.name == "aac"

    def test_音声トラックをすべて残して音ズレしない(self, offset_mkv, tmp_path):
        out = cut(offset_mkv, [Range(frame(100), frame(170)), Range(frame(400), frame(500))], tmp_path / "out.mkv")
        assert stream_types(out) == ["video", "audio", "audio"]
        assert frame_indices(out) == list(range(100, 170)) + list(range(400, 500))
        assert_in_sync(out, audio_index=0)
        assert_in_sync(out, audio_index=1)

    def test_映像が遅れて始まる動画でもフレーム単位で切る(self, delayed_video_mp4, tmp_path):
        out = cut(delayed_video_mp4, [Range(0.25 + frame(50), 0.25 + frame(130))], tmp_path / "out.mp4")
        assert frame_indices(out) == list(range(50, 130))
        assert_in_sync(out)

    def test_既定のエンコーダーでも書き出せる(self, indexed_mp4, tmp_path):
        reencode_cut(str(indexed_mp4), [OutputSpec(str(tmp_path / "out.mp4"), (Range(frame(30), frame(90)),))])
        assert frame_indices(tmp_path / "out.mp4") == list(range(30, 90))

    def test_進捗を0から1まで通知する(self, indexed_mp4, tmp_path):
        values = []
        reencode_cut(
            str(indexed_mp4),
            [OutputSpec(str(tmp_path / "out.mp4"), (Range(0, 2.0),))],
            on_progress=values.append,
            video_encoders=X264,
        )
        assert values[-1] == 1.0
        assert values == sorted(values)

    def test_中止すると出力ファイルも一時ファイルも残さない(self, indexed_mp4, tmp_path):
        from videocut.exporting import Cancelled

        out = tmp_path / "out.mp4"
        with pytest.raises(Cancelled):
            reencode_cut(
                str(indexed_mp4),
                [OutputSpec(str(out), (Range(0, 10.0),))],
                should_cancel=lambda: True,
                video_encoders=X264,
            )
        assert list(tmp_path.iterdir()) == []


class TestChooseVideoEncoder:
    def test_使えないエンコーダーは飛ばして次の候補を使う(self):
        choice = choose_video_encoder(320, 180, "yuv420p", [("no_such_encoder", {}), *X264])
        assert choice.name == "libx264"
        assert choice.pix_fmt == "yuv420p"

    def test_候補がすべて使えなければReencodeErrorにする(self):
        with pytest.raises(ReencodeError):
            choose_video_encoder(320, 180, "yuv420p", [("no_such_encoder", {})])
