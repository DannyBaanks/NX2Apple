"""Linux preparation, explicit runtime selection, and a real Vulkan check in the Qt UI."""
from __future__ import annotations

import codecs
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from . import engine, library, linux_runtime as rt
from .linux_player import LinuxGameWindow, LinuxLiveSession
from .widgets import CardGroup, CardPageHeading


class LinuxPage(QWidget):
    idle = Signal()

    def __init__(self, model):
        super().__init__()
        self.m = model
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self._decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._action = ''
        self._log_file = None
        self._stopping = False
        self._live_session: LinuxLiveSession | None = None
        self._game_window: LinuxGameWindow | None = None
        self._texts = []
        self._inputs = []
        col = QVBoxLayout(self)
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(18)
        self.heading = CardPageHeading()
        col.addWidget(self.heading)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setObjectName('muted')
        col.addWidget(self.warning)
        group = CardGroup()
        col.addWidget(group)
        self.nsp = self._field(group, 'linux.nsp', 'nsp', 'file')
        self.keys = self._field(group, 'linux.keys', 'keys', 'file')
        self.work = self._field(group, 'linux.work', 'work', 'directory')
        self.qemu = self._field(group, 'linux.qemu', 'qemu', 'file')
        self.nsp.textChanged.connect(self._default_work)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.setObjectName('launcherDropdown')
        for key in ('native', 'qemu', 'inspect'):
            self.mode.addItem('', key)
        self.mode.setCurrentIndex(1 if rt.machine_arch() == 'x86_64' else 0)
        self.mode.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.mode, 1)
        self.arch = QComboBox()
        self.arch.setObjectName('launcherDropdown')
        self.arch.addItems(['aarch64', 'x86_64'])
        self.arch.setCurrentText(rt.machine_arch())
        row.addWidget(self.arch)
        col.addLayout(row)
        buttons = QHBoxLayout()
        self.prepare_btn = self._button('linux.prepare', self.prepare)
        self.build_btn = self._button('linux.build', self.build)
        self.run_btn = self._button('linux.run', self.start_run)
        self.render_btn = self._button('linux.render', self.check_render)
        self.stop_btn = self._button('btn.stop', self.stop)
        for b in (self.prepare_btn, self.build_btn, self.run_btn, self.render_btn, self.stop_btn):
            buttons.addWidget(b)
        col.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        col.addWidget(self.status)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.hide()
        col.addWidget(self.preview)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setMinimumHeight(220)
        col.addWidget(self.log)
        self.nsp.textChanged.connect(self.refresh)
        self.keys.textChanged.connect(self.refresh)
        self.work.textChanged.connect(self.refresh)
        self.qemu.textChanged.connect(self.refresh)
        self.refresh()

    @property
    def busy(self):
        return self.process.state() != QProcess.NotRunning

    def _button(self, key, callback):
        b = QPushButton()
        b.clicked.connect(callback)
        self._texts.append((b, key))
        return b

    def _field(self, group, key, setting, kind):
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(16, 12, 16, 12)
        label = QLabel()
        label.setMinimumWidth(110)
        self._texts.append((label, key))
        row.addWidget(label)
        edit = QLineEdit(self.m.settings.value('linux/' + setting, '', type=str))
        edit.setObjectName('linux_' + setting)
        row.addWidget(edit, 1)
        edit.setStyleSheet('QLineEdit { background: #111214; color: #eeeeee; border: 1px solid #343539; border-radius: 6px; padding: 8px; }')
        choose = self._button('linux.browse', lambda: self._pick(edit, kind))
        row.addWidget(choose)
        group.add_row(host)
        edit.editingFinished.connect(lambda: self.m.settings.setValue('linux/' + setting, edit.text()))
        self._inputs += [edit, choose]
        return edit

    def _pick(self, edit, kind):
        if kind == 'directory':
            path = QFileDialog.getExistingDirectory(self, self.m.t('repo.choose'), edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, self.m.t('repo.choose'), edit.text())
        if path:
            edit.setText(path)
            edit.editingFinished.emit()

    def _default_work(self):
        if self.nsp.text() and not self.work.text():
            p = Path(self.nsp.text())
            self.work.setText(str(p.parent / (p.stem + '-linux')))

    def refresh(self):
        t = self.m.t
        self.heading.set_text(t('linux.title'), t('linux.subtitle'))
        self.warning.setText(t('linux.experimental'))
        for obj, key in self._texts:
            obj.setText(t(key))
        for i, key in enumerate(('linux.mode.native', 'linux.mode.qemu', 'linux.mode.inspect')):
            self.mode.setItemText(i, t(key))
        for w in self._inputs + [self.mode, self.arch]:
            w.setEnabled(not self.busy)
        ready = bool(self.work.text() and (Path(self.work.text()) / 'linux-game.json').is_file())
        self.prepare_btn.setEnabled(not self.busy and Path(self.nsp.text()).is_file() and Path(self.keys.text()).is_file() and bool(self.work.text()))
        self.build_btn.setEnabled(not self.busy and ready)
        self.render_btn.setEnabled(not self.busy)
        self.stop_btn.setEnabled(self.busy and not self._stopping)
        reason = ''
        if ready and not self.busy:
            try:
                self.plan()
            except (OSError, ValueError, KeyError) as e:
                reason = str(e)
        self.run_btn.setEnabled(not self.busy and ready and not reason)
        self.run_btn.setToolTip(reason)
        if not self.busy:
            self.status.setText(reason or t('linux.ready' if ready else 'linux.choose'))

    def plan(self):
        work = Path(self.work.text())
        return rt.launch_plan(Path(self.m.engine_path), *rt.prepared_inputs(work),
                              self.mode.currentData(), qemu=self.qemu.text())

    def start_run(self):
        try:
            self.plan()
            mode = self.mode.currentData()
            extra_env = {}
            if mode in ('native', 'qemu'):
                self._close_live()
                offscreen = os.environ.get('QT_QPA_PLATFORM') == 'offscreen'
                self._live_session = LinuxLiveSession(enable_audio=not offscreen)
                extra_env = self._live_session.env_overrides()
                work = Path(self.work.text())
                title = work.name.removesuffix('-linux') or 'NX2Apple Linux'
                self._game_window = LinuxGameWindow(
                    self._live_session,
                    title=title,
                    screenshots_dir=work / 'screenshots',
                )
                self._game_window.stop_requested.connect(self.stop)
                if not offscreen:
                    self._game_window.show()
                    self._game_window.activateWindow()
                    self._game_window.setFocus()
            self._command('run', ['run', '--work', self.work.text(), '--mode', mode, '--qemu', self.qemu.text()],
                          extra_env=extra_env)
        except (OSError, ValueError, KeyError) as e:
            self._close_live()
            self.status.setText(str(e))

    def _command(self, action, args, extra_env=None):
        script = Path(__file__).resolve().parents[2] / 'tools/linux_runtime.py'
        self.start(action, [sys.executable, str(script), '--engine', self.m.engine_path, *args], extra_env=extra_env)

    def prepare(self):
        self._command('prepare', ['prepare', '--nsp', self.nsp.text(), '--keys', self.keys.text(), '--work', self.work.text()])

    def build(self):
        self._command('build', ['build', '--work', self.work.text(), '--arch', self.arch.currentText()])

    def check_render(self):
        self._command('render', ['check-render', '--work', str(library.default_state_dir() / 'linux-render')])

    def start(self, action, argv, extra_env=None):
        if self.busy:
            return
        self._action, self._stopping = action, False
        self._decoder.reset()
        logs = library.default_state_dir() / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        self.log_path = logs / (datetime.now(timezone.utc).strftime('linux-%Y%m%d-%H%M%S-%f.log'))
        self._log_file = self.log_path.open('w', encoding='utf-8')
        self.log.clear()
        env = QProcessEnvironment()
        merged = rt.child_env(extra_env)
        for k, v in merged.items():
            env.insert(k, v)
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(Path(self.m.repo_path)))
        self.process.start(argv[0], argv[1:])
        self.refresh()
        self.status.setText(self.m.t('linux.running', action, str(self.log_path)))

    def _close_live(self):
        if self._game_window is not None:
            win = self._game_window
            self._game_window = None
            win.stop_polling()
            try:
                win.stop_requested.disconnect(self.stop)
            except (TypeError, RuntimeError):
                pass
            win.close()
            win.deleteLater()
        if self._live_session is not None:
            sess = self._live_session
            self._live_session = None
            sess.close()

    def _read(self):
        text = self._decoder.decode(bytes(self.process.readAllStandardOutput()))
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        if self._log_file:
            self._log_file.write(text)
            self._log_file.flush()

    def _error(self, error):
        if error == QProcess.FailedToStart:
            self._finished(-1, QProcess.CrashExit)

    def _finished(self, code, status):
        self._read()
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        self._close_live()
        self.refresh()
        self.status.setText(self.m.t('linux.finished', code))
        if code == 0 and self._action == 'render':
            p = library.default_state_dir() / 'linux-render/vulkan-replay.ppm'
            pixmap = QPixmap(str(p))
            if not pixmap.isNull():
                self.preview.setPixmap(pixmap.scaled(512, 384, Qt.KeepAspectRatio, Qt.FastTransformation))
                self.preview.setToolTip(self.m.t('linux.testFrame'))
                self.preview.show()
        self.idle.emit()

    def stop(self):
        if not self.busy:
            return
        self._stopping = True
        pid = int(self.process.processId())
        self._signal_group(pid, signal.SIGTERM)
        QTimer.singleShot(3000, self, lambda: self._kill_if_running(pid))
        self.refresh()

    def _signal_group(self, pid, sig):
        # QProcess created this session. Never derive or signal a parent PID.
        try:
            if pid > 1 and os.getsid(pid) == pid:
                os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def _kill_if_running(self, pid):
        if self.busy and int(self.process.processId()) == pid:
            self._signal_group(pid, signal.SIGKILL)
