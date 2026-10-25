"""メインウィンドウ: プレビュー・タイムライン・区間の一覧・書き出し"""

import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from videocut.media import KeyframeIndex, MediaError, MediaInfo, probe
from videocut.plan import Job, Method, build_outputs, default_output_path, estimate_output_bytes
from videocut.ranges import (
    SNAP_TOLERANCE,
    Mode,
    Range,
    keep_ranges,
    normalize,
    snap_inward,
    snap_outward,
    total_length,
)
from videocut.timecode import format_time, parse_time
from videocut.ui.export_runner import ExportRunner
from videocut.ui.timeline import Timeline

APP_TITLE = "動画カットツール"
VIDEO_FILTER = "動画ファイル (*.mp4 *.mkv *.mov *.m4v *.ts *.m2ts *.mts *.flv *.webm *.avi);;すべてのファイル (*)"
SHORTCUT_HELP = (
    "Space 再生/停止 ・ 左右キー 1コマ ・ Shift+左右 1秒 ・ Ctrl+左右 10秒 ・ 上下キー 前/次のキーフレーム ・ "
    "I 開始点 ・ O 終了点 ・ Del 区間を削除 ・ タイムライン: ホイールで拡大縮小、区間の端をドラッグで調整"
)
_CODEC_NAMES = {"h264": "H.264", "hevc": "H.265", "av1": "AV1"}
# コマ送りの移動先を、この秒数のあいだはフレームの到着前でも次の基準にする（連打で取りこぼさない）
_PENDING_SEEK_SECONDS = 0.5


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 880)
        self.setAcceptDrops(True)
        self.info: MediaInfo | None = None
        self.keyframes: KeyframeIndex | None = None
        self.ranges: list[Range] = []
        self.pending_start: float | None = None
        self.frame_time: float | None = None  # 表示中のフレームの開始時刻（コンテナ先頭からの秒）
        self._pending_seek: tuple[float, float] | None = None  # (移動先のフレーム, 指示した時刻)
        self._keyframe_interval: float | None = None
        self._export_started = 0.0
        self._export_paths: list[str] = []

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.video = QVideoWidget()
        self.player.setVideoOutput(self.video)
        self.video.videoSink().videoFrameChanged.connect(self._on_video_frame)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.tracksChanged.connect(self._update_audio_tracks)

        self.runner = ExportRunner(self)
        self.runner.progressChanged.connect(self._on_export_progress)
        self.runner.succeeded.connect(self._on_export_succeeded)
        self.runner.failed.connect(self._on_export_failed)
        self.runner.canceled.connect(self._on_export_canceled)

        self._build_ui()
        self._build_shortcuts()
        self._set_media_controls_enabled(False)

    # ---- 画面の組み立て ----

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.open_button = QPushButton("動画を開く…")
        self.open_button.clicked.connect(self.choose_file)
        self.info_label = QLabel("動画ファイルを開くか、この画面にドラッグ＆ドロップしてください")
        top.addWidget(self.open_button)
        top.addWidget(self.info_label, 1)
        root.addLayout(top)

        self.video.setMinimumHeight(240)
        root.addWidget(self.video, 1)

        self.timeline = Timeline()
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.rangeClicked.connect(self.table_select)
        self.timeline.rangeEdited.connect(self._on_range_edited)
        root.addWidget(self.timeline)

        root.addLayout(self._build_transport())

        bottom = QHBoxLayout()
        bottom.addWidget(self._build_ranges_group(), 3)
        bottom.addWidget(self._build_export_group(), 2)
        root.addLayout(bottom)

        help_label = QLabel(SHORTCUT_HELP)
        help_label.setStyleSheet("color: gray;")
        help_label.setWordWrap(True)
        root.addWidget(help_label)
        self.setCentralWidget(central)

    def _build_transport(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.transport_buttons = []
        for text, tip, slot in (
            ("|◀ 先頭", "先頭へ [Home]", lambda: self.seek(0.0)),
            ("◀◀ キーフレーム", "前のキーフレームへ [上キー]", lambda: self.jump_keyframe(-1)),
            ("◀ 1コマ", "1コマ戻る [左キー]", lambda: self.step_frames(-1)),
        ):
            bar.addWidget(self._transport_button(text, tip, slot))
        self.play_button = self._transport_button("▶ 再生", "再生 / 一時停止 [Space]", self.toggle_play)
        bar.addWidget(self.play_button)
        for text, tip, slot in (
            ("1コマ ▶", "1コマ進む [右キー]", lambda: self.step_frames(1)),
            ("キーフレーム ▶▶", "次のキーフレームへ [下キー]", lambda: self.jump_keyframe(1)),
            ("末尾 ▶|", "末尾へ [End]", self.seek_end),
        ):
            bar.addWidget(self._transport_button(text, tip, slot))

        self.time_edit = QLineEdit(format_time(0))
        self.time_edit.setFixedWidth(110)
        self.time_edit.setToolTip("時刻を入力して Enter で移動（例: 1:23:45.5）")
        self.time_edit.returnPressed.connect(self._on_time_entered)
        self.time_label = QLabel("/ " + format_time(0))
        bar.addSpacing(12)
        bar.addWidget(self.time_edit)
        bar.addWidget(self.time_label)
        bar.addStretch(1)

        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setToolTip("プレビューで聞く音声トラック（書き出しには全トラックを残します）")
        self.audio_track_combo.currentIndexChanged.connect(self._on_audio_track_selected)
        self.audio_track_combo.setVisible(False)
        bar.addWidget(self.audio_track_combo)
        bar.addWidget(QLabel("音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100))
        bar.addWidget(self.volume_slider)
        return bar

    def _transport_button(self, text: str, tip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        self.transport_buttons.append(button)
        return button

    def _build_ranges_group(self) -> QGroupBox:
        group = QGroupBox("区間")
        layout = QVBoxLayout(group)
        buttons = QGridLayout()
        self.mark_start_button = QPushButton("開始点 [I]")
        self.mark_start_button.clicked.connect(self.mark_start)
        self.mark_end_button = QPushButton("終了点 [O]")
        self.mark_end_button.clicked.connect(self.mark_end)
        self.delete_button = QPushButton("選択した区間を削除 [Del]")
        self.delete_button.clicked.connect(self.delete_selected)
        self.set_start_button = QPushButton("選択した区間の開始を現在位置に")
        self.set_start_button.clicked.connect(self.set_selected_start)
        self.set_end_button = QPushButton("選択した区間の終了を現在位置に")
        self.set_end_button.clicked.connect(self.set_selected_end)
        self.clear_button = QPushButton("すべて削除")
        self.clear_button.clicked.connect(self.clear_ranges)
        buttons.addWidget(self.mark_start_button, 0, 0)
        buttons.addWidget(self.mark_end_button, 0, 1)
        buttons.addWidget(self.delete_button, 0, 2)
        buttons.addWidget(self.set_start_button, 1, 0)
        buttons.addWidget(self.set_end_button, 1, 1)
        buttons.addWidget(self.clear_button, 1, 2)
        layout.addLayout(buttons)

        self.pending_label = QLabel("")
        self.pending_label.setStyleSheet("color: #c9a227;")
        layout.addWidget(self.pending_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["開始", "終了", "長さ", "無劣化で実際に切る範囲"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.cellDoubleClicked.connect(lambda row, _col: self.seek_frame(self.ranges[row].start))
        self.table.setToolTip("ダブルクリックで区間の先頭へ移動")
        layout.addWidget(self.table)
        return group

    def _build_export_group(self) -> QGroupBox:
        group = QGroupBox("書き出し")
        layout = QVBoxLayout(group)

        self.extract_radio = QRadioButton("抜き出し — 選んだ区間を残す")
        self.delete_radio = QRadioButton("削除 — 選んだ区間を消して残りをつなげる")
        self.extract_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        for radio in (self.extract_radio, self.delete_radio):
            self._mode_group.addButton(radio)
            layout.addWidget(radio)
        self._mode_group.buttonToggled.connect(lambda *_: self._refresh_ranges())

        self.copy_radio = QRadioButton("無劣化・高速（キーフレーム単位で切る）")
        self.reencode_radio = QRadioButton("正確（フレーム単位で切る・再エンコード）")
        self.copy_radio.setChecked(True)
        self._method_group = QButtonGroup(self)
        for radio in (self.copy_radio, self.reencode_radio):
            self._method_group.addButton(radio)
            layout.addWidget(radio)
        self._method_group.buttonToggled.connect(lambda *_: self._refresh_ranges())

        self.method_note = QLabel("")
        self.method_note.setWordWrap(True)
        self.method_note.setStyleSheet("color: gray;")
        layout.addWidget(self.method_note)

        self.separate_check = QCheckBox("区間ごとに別のファイルにする（抜き出しのみ）")
        layout.addWidget(self.separate_check)

        self.summary_label = QLabel("")
        layout.addWidget(self.summary_label)

        row = QHBoxLayout()
        self.export_button = QPushButton("書き出し…")
        self.export_button.clicked.connect(self.export)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.clicked.connect(self.runner.cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.export_button, 1)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        self.export_status = QLabel("")
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        layout.addStretch(1)
        return group

    def _build_shortcuts(self) -> None:
        for key, slot in (
            ("Space", self.toggle_play),
            ("Left", lambda: self.step_frames(-1)),
            ("Right", lambda: self.step_frames(1)),
            ("Shift+Left", lambda: self.step_seconds(-1)),
            ("Shift+Right", lambda: self.step_seconds(1)),
            ("Ctrl+Left", lambda: self.step_seconds(-10)),
            ("Ctrl+Right", lambda: self.step_seconds(10)),
            ("Up", lambda: self.jump_keyframe(-1)),
            ("Down", lambda: self.jump_keyframe(1)),
            ("Home", lambda: self.seek(0.0)),
            ("End", self.seek_end),
            ("I", self.mark_start),
            ("O", self.mark_end),
            ("Delete", self.delete_selected),
            ("Escape", self.cancel_pending),
            ("Ctrl+O", self.choose_file),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

    def _set_media_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            *self.transport_buttons,
            self.time_edit,
            self.mark_start_button,
            self.mark_end_button,
            self.delete_button,
            self.set_start_button,
            self.set_end_button,
            self.clear_button,
            self.export_button,
        ):
            widget.setEnabled(enabled)

    # ---- ファイル ----

    def choose_file(self) -> None:
        start = str(Path(self.info.path).parent) if self.info else QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.MoviesLocation
        )
        path, _ = QFileDialog.getOpenFileName(self, "動画を開く", start, VIDEO_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        try:
            info = probe(path)
            keyframes = KeyframeIndex(path, info)
        except MediaError as e:
            QMessageBox.critical(self, "動画を開けません", str(e))
            return
        if self.ranges and QMessageBox.question(
            self, "確認", "別の動画を開くと、いま設定している区間は消えます。開きますか？"
        ) != QMessageBox.StandardButton.Yes:
            keyframes.close()
            return
        if self.keyframes:
            self.keyframes.close()
        self.info, self.keyframes = info, keyframes
        self.ranges, self.pending_start, self.frame_time, self._pending_seek = [], None, None, None
        self._keyframe_interval = _keyframe_interval(info, keyframes)

        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        self.timeline.set_duration(info.duration)
        self.timeline.set_frame_snap(self.snap_to_frame, info.video.frame_duration)
        self.timeline.set_position(0.0)
        self.info_label.setText(_describe(info))
        self.time_label.setText("/ " + format_time(info.duration))
        self.setWindowTitle(f"{Path(path).name} - {APP_TITLE}")
        self._set_media_controls_enabled(True)
        self._refresh_ranges()

    def dragEnterEvent(self, event) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if files:
            self.open_file(files[0])

    # ---- 再生と移動 ----

    def current_time(self) -> float:
        if self.frame_time is not None:
            return self.frame_time
        return self.player.position() / 1000

    def snap_to_frame(self, t: float) -> float:
        """最も近いフレームの開始時刻"""
        video = self.info.video
        return video.start + round((t - video.start) / video.frame_duration) * video.frame_duration

    def seek(self, t: float) -> None:
        if self.info is None:
            return
        t = min(max(t, 0.0), max(0.0, self.info.duration - 0.001))
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()
        self.player.setPosition(round(t * 1000))
        self.timeline.set_position(t)

    def seek_frame(self, frame_start: float) -> None:
        """フレームの開始時刻を受け取り、フレームの中央へ移動して確実にそのフレームを表示する"""
        self._pending_seek = (frame_start, time.monotonic())
        self.seek(frame_start + self.info.video.frame_duration / 2)

    def seek_end(self) -> None:
        if self.info:
            self.seek_frame(self._last_frame_start())

    def _last_frame_start(self) -> float:
        return self.snap_to_frame(self.info.duration - self.info.video.frame_duration)

    def _base_time(self) -> float:
        """次の移動の基準。直前に指示した移動先がまだ表示されていなければそちらを使う"""
        if self._pending_seek and time.monotonic() - self._pending_seek[1] < _PENDING_SEEK_SECONDS:
            return self._pending_seek[0]
        return self.current_time()

    def toggle_play(self) -> None:
        if self.info is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._pending_seek = None
            self.player.play()

    def step_frames(self, count: int) -> None:
        if self.info is None:
            return
        self.player.pause()
        target = self.snap_to_frame(self._base_time()) + count * self.info.video.frame_duration
        self.seek_frame(min(max(target, self.info.video.start), self._last_frame_start()))

    def step_seconds(self, seconds: float) -> None:
        if self.info is None:
            return
        target = self.snap_to_frame(self._base_time() + seconds)
        self.seek_frame(min(max(target, self.info.video.start), self._last_frame_start()))

    def jump_keyframe(self, direction: int) -> None:
        if self.keyframes is None:
            return
        self.player.pause()
        base = self._base_time()
        if direction > 0:
            target = self.keyframes.after(base + SNAP_TOLERANCE)
        else:
            target = self.keyframes.at_or_before(base - SNAP_TOLERANCE)
        if target is not None:
            self.seek_frame(target)

    def _on_time_entered(self) -> None:
        try:
            t = parse_time(self.time_edit.text())
        except ValueError as e:
            self.statusBar().showMessage(str(e), 5000)
            return
        if self.info:
            self.seek_frame(self.snap_to_frame(min(t, self.info.duration)))
        self.time_edit.clearFocus()

    def _on_video_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid() or frame.startTime() < 0:
            return
        self.frame_time = frame.startTime() / 1e6
        if self._pending_seek and abs(self._pending_seek[0] - self.frame_time) < 1e-3:
            self._pending_seek = None
        if not self.time_edit.hasFocus():
            self.time_edit.setText(format_time(self.frame_time))
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.timeline.set_position(self.frame_time, follow=playing)

    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("❚❚ 一時停止" if playing else "▶ 再生")

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()

    def _on_player_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            self.statusBar().showMessage(f"プレビューのエラー: {message}", 10000)

    def _update_audio_tracks(self) -> None:
        tracks = self.player.audioTracks()
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        for i, meta in enumerate(tracks):
            title = meta.stringValue(QMediaMetaData.Key.Title) or meta.stringValue(QMediaMetaData.Key.Language)
            self.audio_track_combo.addItem(f"音声 {i + 1}" + (f": {title}" if title else ""))
        self.audio_track_combo.setCurrentIndex(max(0, self.player.activeAudioTrack()))
        self.audio_track_combo.blockSignals(False)
        self.audio_track_combo.setVisible(len(tracks) > 1)

    def _on_audio_track_selected(self, index: int) -> None:
        if index >= 0:
            self.player.setActiveAudioTrack(index)

    # ---- 区間 ----

    def mode(self) -> Mode:
        return Mode.EXTRACT if self.extract_radio.isChecked() else Mode.DELETE

    def method(self) -> Method:
        return Method.COPY if self.copy_radio.isChecked() else Method.REENCODE

    def set_ranges(self, ranges: list[Range]) -> None:
        if self.info is None:
            return
        self.ranges = normalize(ranges, self.info.duration, min_length=self.info.video.frame_duration / 2)
        self._refresh_ranges()

    def mark_start(self) -> None:
        if self.info is None:
            return
        self.pending_start = self.current_time()
        self._refresh_ranges()

    def mark_end(self) -> None:
        if self.info is None:
            return
        if self.pending_start is None:
            self.statusBar().showMessage("先に開始点（I）を設定してください", 5000)
            return
        start, end = self.pending_start, self.current_time() + self.info.video.frame_duration
        if end <= start + 1e-6:
            self.statusBar().showMessage("終了点は開始点より後ろにしてください", 5000)
            return
        self.pending_start = None
        self.set_ranges([*self.ranges, Range(start, end)])
        index = next((i for i, r in enumerate(self.ranges) if r.start <= start < r.end), None)
        if index is not None:
            self.table_select(index)

    def cancel_pending(self) -> None:
        self.pending_start = None
        self._refresh_ranges()

    def selected_index(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def table_select(self, index: int) -> None:
        self.table.selectRow(index)

    def delete_selected(self) -> None:
        index = self.selected_index()
        if index is not None:
            self.set_ranges(self.ranges[:index] + self.ranges[index + 1 :])

    def clear_ranges(self) -> None:
        if self.ranges and QMessageBox.question(self, "確認", "すべての区間を削除しますか？") == QMessageBox.StandardButton.Yes:
            self.set_ranges([])

    def set_selected_start(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        r, start = self.ranges[index], self.current_time()
        if start >= r.end:
            self.statusBar().showMessage("開始は区間の終了より前にしてください", 5000)
            return
        self._replace_range(index, Range(start, r.end))

    def set_selected_end(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        r, end = self.ranges[index], self.current_time() + self.info.video.frame_duration
        if end <= r.start:
            self.statusBar().showMessage("終了は区間の開始より後ろにしてください", 5000)
            return
        self._replace_range(index, Range(r.start, end))

    def _on_range_edited(self, index: int, start: float, end: float) -> None:
        self._replace_range(index, Range(start, end))

    def _replace_range(self, index: int, new: Range) -> None:
        ranges = list(self.ranges)
        ranges[index] = new
        self.set_ranges(ranges)
        self.table_select(next((i for i, r in enumerate(self.ranges) if r.start <= new.start < r.end), 0))

    def _on_table_selection(self) -> None:
        self.timeline.set_selected(self.selected_index())

    def _update_method_note(self) -> None:
        if self.info is None:
            text = ""
        elif self.method() is Method.COPY:
            interval = self._keyframe_interval
            hint = f"この動画では約 {interval:.1f} 秒間隔" if interval else "間隔は動画によって異なります"
            edge = "指定より少し外側まで残ります" if self.mode() is Mode.EXTRACT else "指定より少し内側だけを消します"
            text = f"画質はそのままで、ほぼファイルのコピーの速さで終わります。切れ目をキーフレーム（{hint}）に合わせるため、{edge}。"
        else:
            text = (
                "指定したフレームちょうどで切ります。残す部分をすべて再エンコードするので時間がかかり、"
                "画質もわずかに落ちます（NVIDIA などの GPU があれば使います）。"
            )
        self.method_note.setText(text)

    def result_ranges(self) -> list[Range]:
        """書き出し後に残る範囲（無劣化ならキーフレームまで広げたもの）"""
        if self.info is None or not self.ranges:
            return []
        keep = keep_ranges(self.mode(), self.ranges, self.info.duration, min_length=self.info.video.frame_duration / 2)
        if self.method() is Method.COPY and keep:
            return snap_outward(keep, self.keyframes, self.info.duration)
        return keep

    def _actual_text(self, r: Range) -> str:
        if self.mode() is Mode.EXTRACT:
            actual = snap_outward([r], self.keyframes, self.info.duration)[0]
        else:
            actual = snap_inward(r, self.keyframes, self.info.duration)
            if actual is None:
                return "削除されません（キーフレームの間隔より短い）"
        if abs(actual.start - r.start) < SNAP_TOLERANCE and abs(actual.end - r.end) < SNAP_TOLERANCE:
            return "指定どおり"
        return f"{format_time(actual.start)} – {format_time(actual.end)}"

    def _refresh_ranges(self) -> None:
        mode = self.mode()
        result = self.result_ranges()
        self._update_method_note()
        self.timeline.set_ranges(self.ranges, mode)
        self.timeline.set_result(result)
        self.timeline.set_pending_start(self.pending_start)

        selected = self.selected_index()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.ranges))
        copy = self.method() is Method.COPY
        for row, r in enumerate(self.ranges):
            cells = [format_time(r.start), format_time(r.end), format_time(r.length), self._actual_text(r) if copy else ""]
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.setColumnHidden(3, not copy)
        if selected is not None and selected < len(self.ranges):
            self.table.selectRow(selected)
        self.table.blockSignals(False)
        self._on_table_selection()

        if self.pending_start is not None:
            self.pending_label.setText(f"開始点 {format_time(self.pending_start)} — 終了位置で O を押すと区間になります（Esc で取り消し）")
        else:
            self.pending_label.setText("")

        if self.info is None:
            self.summary_label.setText("")
        elif not self.ranges:
            self.summary_label.setText("区間を追加してください（I で開始点、O で終了点）")
        elif not result:
            self.summary_label.setText("書き出す範囲がありません")
        else:
            self.summary_label.setText(f"書き出し後の長さ: {format_time(total_length(result))}（{len(result)} 区間）")
        self.separate_check.setEnabled(mode is Mode.EXTRACT and not self.runner.is_running())
        self.export_button.setEnabled(bool(result) and not self.runner.is_running())

    # ---- 書き出し ----

    def export(self) -> None:
        if self.info is None or self.runner.is_running():
            return
        result = self.result_ranges()
        if not result:
            QMessageBox.warning(self, "書き出せません", "書き出す範囲がありません。区間を追加してください。")
            return
        source = self.info.path
        suffix = Path(source).suffix or ".mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "書き出し先を選ぶ", default_output_path(source), f"元と同じ形式 (*{suffix});;すべてのファイル (*)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += suffix
        separate = self.mode() is Mode.EXTRACT and self.separate_check.isChecked()
        outputs = build_outputs(result, separate, path)

        if any(_same_path(o.path, source) for o in outputs):
            QMessageBox.critical(self, "書き出せません", "元の動画と同じファイルには書き出せません。別の名前を指定してください。")
            return
        existing = [o.path for o in outputs if separate and os.path.exists(o.path)]
        if existing and QMessageBox.question(
            self, "上書きの確認", f"{len(existing)} 個のファイルがすでにあります。上書きしますか？\n" + "\n".join(existing[:5])
        ) != QMessageBox.StandardButton.Yes:
            return
        if not self._confirm_disk_space(outputs[0].path, result):
            return

        self._export_paths = [o.path for o in outputs]
        self._export_started = time.monotonic()
        self.runner.start(Job(source, self.method(), outputs))
        self._set_exporting(True)
        self.export_status.setText("書き出しを開始しました…")

    def _confirm_disk_space(self, path: str, result: list[Range]) -> bool:
        info = self.info
        need = estimate_output_bytes(
            self.method(), info.size, info.duration, total_length(result), info.video.bit_rate, [a.bit_rate for a in info.audio]
        )
        free = shutil.disk_usage(Path(path).parent).free
        if need <= free:
            return True
        return QMessageBox.question(
            self,
            "空き容量の確認",
            f"書き出し先の空き容量が足りない可能性があります。\n必要な容量の目安: {_gigabytes(need)}\n空き容量: {_gigabytes(free)}\n\n続けますか？",
        ) == QMessageBox.StandardButton.Yes

    def _set_exporting(self, busy: bool) -> None:
        for widget in (self.extract_radio, self.delete_radio, self.copy_radio, self.reencode_radio):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress_bar.setValue(0)
        self._refresh_ranges()

    def _on_export_progress(self, value: float) -> None:
        self.progress_bar.setValue(round(value * 1000))
        elapsed = time.monotonic() - self._export_started
        text = f"書き出し中… {value:.0%}"
        if value >= 0.02:
            text += f"（残り 約 {_duration_text(elapsed * (1 - value) / value)}）"
        self.export_status.setText(text)

    def _on_export_succeeded(self, outputs: list) -> None:
        self._set_exporting(False)
        self.progress_bar.setValue(1000)
        elapsed = _duration_text(time.monotonic() - self._export_started)
        self.export_status.setText(f"完了しました（{elapsed}）: {Path(outputs[0]).name}" + (f" ほか {len(outputs) - 1} 個" if len(outputs) > 1 else ""))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("書き出し完了")
        box.setText("書き出しが完了しました。\n\n" + "\n".join(outputs[:10]) + ("\n…" if len(outputs) > 10 else ""))
        open_folder = box.addButton("フォルダを開く", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(outputs[0]).parent)))

    def _on_export_failed(self, message: str, detail: str) -> None:
        self._set_exporting(False)
        self.export_status.setText("書き出しに失敗しました")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("書き出しに失敗しました")
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
        box.exec()

    def _on_export_canceled(self) -> None:
        self._set_exporting(False)
        self.progress_bar.setValue(0)
        done = [p for p in self._export_paths if os.path.exists(p)]
        self.export_status.setText("中止しました" + (f"（書き出し済みの {len(done)} ファイルは残しています）" if done else ""))

    def closeEvent(self, event) -> None:
        if self.runner.is_running():
            if QMessageBox.question(self, "確認", "書き出し中です。中止して終了しますか？") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.runner.cancel_and_wait()
        self.player.stop()
        if self.keyframes:
            self.keyframes.close()
            self.keyframes = None
        super().closeEvent(event)


def _keyframe_interval(info: MediaInfo, keyframes: KeyframeIndex) -> float | None:
    """動画の中ほどのキーフレーム間隔（説明の表示用）"""
    middle = info.duration / 2
    before, after = keyframes.at_or_before(middle), keyframes.after(middle)
    return after - before if before is not None and after is not None else None


def _describe(info: MediaInfo) -> str:
    video = info.video
    fps = f"{int(video.fps)}fps" if video.fps.denominator == 1 else f"{float(video.fps):.2f}fps"
    codec = _CODEC_NAMES.get(video.codec, video.codec.upper())
    return (
        f"{Path(info.path).name}　{video.width}x{video.height} {fps} {codec} ・ "
        f"音声 {len(info.audio)} トラック ・ {format_time(info.duration)} ・ {_gigabytes(info.size)}"
    )


def _gigabytes(size: float) -> str:
    return f"{size / 1024**3:.2f} GB"


def _duration_text(seconds: float) -> str:
    seconds = max(0, round(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"


def _same_path(a: str, b: str) -> bool:
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:
        return os.path.samefile(a, b)  # 別名（ハードリンクなど）で同じファイルを指している場合
    except OSError:
        return False  # 書き出し先がまだ無い
