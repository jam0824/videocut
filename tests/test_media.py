import pytest

from media_factory import decode_frames, make_indexed_video
from videocut.media import KeyframeIndex, MediaError, probe


class TestProbe:
    def test_映像と音声の情報を読む(self, indexed_mp4):
        info = probe(str(indexed_mp4))
        assert info.duration == pytest.approx(10.0, abs=0.05)
        assert (info.video.width, info.video.height) == (320, 180)
        assert info.video.fps == 60
        assert info.video.frame_duration == pytest.approx(1 / 60)
        assert info.video.codec == "h264"
        assert len(info.audio) == 1
        assert info.audio[0].sample_rate == 48000
        assert info.size == indexed_mp4.stat().st_size

    def test_複数の音声トラックを読む(self, offset_mkv):
        assert len(probe(str(offset_mkv)).audio) == 2

    def test_映像の開始位置をコンテナ先頭からの秒で返す(self, offset_mkv):
        info = probe(str(offset_mkv))
        first_frame_time = decode_frames(offset_mkv)[0][0]
        assert info.start_time > 1.0
        assert info.video.start == pytest.approx(first_frame_time, abs=1e-3)

    def test_動画ではないファイルはMediaErrorにする(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video" * 100)
        with pytest.raises(MediaError):
            probe(str(bad))

    def test_映像の無いファイルはMediaErrorにする(self, tmp_path):
        import av
        import numpy as np

        path = tmp_path / "audio.m4a"
        with av.open(str(path), "w") as out:
            stream = out.add_stream("aac", rate=48000, layout="stereo")
            frame = av.AudioFrame.from_ndarray(np.zeros((2, 1024), dtype=np.float32), format="fltp", layout="stereo")
            frame.sample_rate = 48000
            for packet in [*stream.encode(frame), *stream.encode()]:
                out.mux(packet)
        with pytest.raises(MediaError):
            probe(str(path))


class TestKeyframeIndex:
    """キーフレームは 0.5 秒ごと（60fps, GOP 30）"""

    @pytest.fixture
    def index(self, indexed_mp4):
        kfi = KeyframeIndex(str(indexed_mp4), probe(str(indexed_mp4)))
        yield kfi
        kfi.close()

    def test_指定時刻以前で最も近いキーフレームを返す(self, index):
        assert index.at_or_before(1.7) == pytest.approx(1.5)

    def test_キーフレームちょうどならそのキーフレームを返す(self, index):
        assert index.at_or_before(2.0) == pytest.approx(2.0)

    def test_指定時刻より後で最も近いキーフレームを返す(self, index):
        assert index.after(1.7) == pytest.approx(2.0)
        assert index.after(2.0) == pytest.approx(2.5)

    def test_最後のキーフレームより後はNoneを返す(self, index):
        assert index.after(9.6) is None
        assert index.at_or_before(9.9) == pytest.approx(9.5)

    def test_先頭より前はNoneを返す(self, index):
        assert index.at_or_before(-0.1) is None
        assert index.after(-0.1) == pytest.approx(0.0)

    def test_同じ区間を何度調べても同じ結果になる(self, index):
        first = [index.at_or_before(t) for t in (3.2, 0.1, 7.7, 3.3)]
        second = [index.at_or_before(t) for t in (3.3, 7.7, 0.1, 3.2)]
        assert first == pytest.approx([3.0, 0.0, 7.5, 3.0])
        assert second == pytest.approx([3.0, 7.5, 0.0, 3.0])

    def test_開始がずれたMKVでもコンテナ先頭からの秒で返す(self, offset_mkv):
        info = probe(str(offset_mkv))
        kfi = KeyframeIndex(str(offset_mkv), info)
        try:
            assert kfi.at_or_before(info.video.start + 2.2) == pytest.approx(info.video.start + 2.0, abs=1e-3)
            assert kfi.after(info.video.start + 2.2) == pytest.approx(info.video.start + 2.5, abs=1e-3)
            assert kfi.at_or_before(info.video.start - 0.01) is None
        finally:
            kfi.close()

    def test_長いGOPでも直前のキーフレームを見つける(self, tmp_path):
        path = make_indexed_video(tmp_path / "longgop.mp4", seconds=12, gop=250)
        kfi = KeyframeIndex(str(path), probe(str(path)))
        try:
            assert kfi.at_or_before(8.2) == pytest.approx(250 / 60)
            assert kfi.after(8.2) == pytest.approx(500 / 60)
        finally:
            kfi.close()
