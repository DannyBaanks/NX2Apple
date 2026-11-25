"""Real Qt child lifecycle, log delivery and truthful native-mode availability."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from nspb_qt.i18n import Lang
from nspb_qt.linux_page import LinuxPage


class PageProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        from nspb_qt.l10n import t
        m = mock.Mock()
        m.settings = QSettings(str(root / 'settings.ini'), QSettings.IniFormat)
        m.t = lambda key, *args: t(key, Lang('en'), *args)
        m.repo_path = str(Path(__file__).resolve().parents[2])
        m.engine_path = str(Path(m.repo_path) / 'engine')
        self.env = mock.patch.dict(os.environ, {'NSPB_STATE_DIR': str(root / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.page = LinuxPage(m)
        self.addCleanup(self.page.deleteLater)

    def until(self, predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate(), self.page.log.toPlainText())

    def test_build_target_follows_execution_mode(self):
        with mock.patch('nspb_qt.linux_runtime.machine_arch', return_value='x86_64'):
            page = LinuxPage(self.page.m)
            self.addCleanup(page.deleteLater)
            self.assertEqual(page.mode.currentData(), 'qemu')
            self.assertEqual(page.arch.currentText(), 'aarch64')
            page.mode.setCurrentIndex(page.mode.findData('inspect'))
            self.assertEqual(page.arch.currentText(), 'x86_64')
            page.mode.setCurrentIndex(page.mode.findData('qemu'))
            page.arch.setCurrentText('x86_64')
            with mock.patch.object(page, '_command') as command:
                page.build()
            self.assertEqual(command.call_args.args[1][-1], 'aarch64')

    def test_child_output_and_nonzero_status_are_delivered(self):
        self.page.start('test', [sys.executable, '-c', 'print("real child output"); raise SystemExit(7)'])
        self.until(lambda: not self.page.busy)
        self.assertIn('real child output', self.page.log.toPlainText())
        self.assertIn('7', self.page.status.text())
        self.assertIn('real child output', self.page.log_path.read_text())
        self.assertFalse(self.page.stop_btn.isEnabled())

    def test_stop_terminates_our_child_and_returns_ui_to_idle(self):
        self.page.start('test', [sys.executable, '-u', '-c',
                                'import time; print("ready"); time.sleep(60)'])
        self.until(lambda: 'ready' in self.page.log.toPlainText())
        pid = int(self.page.process.processId())
        self.assertEqual(os.getsid(pid), pid)
        self.page.stop()
        self.until(lambda: not self.page.busy)
        self.assertFalse(Path(f'/proc/{pid}').exists())
        self.assertTrue(self.page.render_btn.isEnabled())

    def test_failed_start_returns_ui_to_idle(self):
        self.page.start('test', ['/no-such-nx2apple-executable'])
        self.until(lambda: not self.page.busy)
        self.assertIn('-1', self.page.status.text())
        self.assertIsNone(self.page._log_file)


class LivePlayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shm_reader_decodes_double_buffered_frame(self):
        import struct
        from nspb_qt.linux_player import (
            FB_SHM_HDR_FMT,
            FB_SHM_HDR_SZ,
            FB_SHM_MAGIC,
            FB_SHM_VERSION,
            ShmFramebufferReader,
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'fb.shm'
            w, h = 4, 2
            stride = w * 4
            buf_bytes = stride * h
            hdr = struct.pack(FB_SHM_HDR_FMT, FB_SHM_MAGIC, FB_SHM_VERSION,
                              w, h, stride, 1, 1, buf_bytes, 7, 12345, 0, 0, w, h)
            slot0 = bytes(buf_bytes)
            slot1 = bytes([10, 20, 30, 255] * (w * h))
            p.write_bytes(hdr + slot0 + slot1)
            reader = ShmFramebufferReader(p)
            try:
                img = reader.poll_image()
                self.assertIsNotNone(img)
                self.assertEqual((img.width(), img.height()), (w, h))
                c = img.pixelColor(0, 0)
                self.assertEqual((c.red(), c.green(), c.blue(), c.alpha()), (10, 20, 30, 255))
                # Same seq returns None
                self.assertIsNone(reader.poll_image())
            finally:
                reader.close()

    def test_game_window_sends_input_packets_over_fifo(self):
        import struct
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from nspb_qt.linux_player import DM_BTN_A, LinuxGameWindow, LinuxLiveSession

        sess = LinuxLiveSession(enable_audio=False)
        self.addCleanup(sess.close)
        rfd = os.open(sess.input_fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(lambda: os.close(rfd))
        win = LinuxGameWindow(sess, title='TestGame')
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.stop_polling)

        press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.NoModifier)
        release = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Z, Qt.KeyboardModifier.NoModifier)
        win.keyPressEvent(press)
        win.keyReleaseEvent(release)

        raw = os.read(rfd, 48)
        self.assertEqual(len(raw), 48)
        b1, *_ = struct.unpack_from('<Qiiii', raw, 0)
        b2, *_ = struct.unpack_from('<Qiiii', raw, 24)
        self.assertEqual(b1, DM_BTN_A)
        self.assertEqual(b2, 0)

    def test_releasing_one_alias_preserves_other_held_keys(self):
        import struct
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent, QFocusEvent
        from nspb_qt.linux_player import DM_BTN_A, DM_BTN_LEFT, LinuxGameWindow, LinuxLiveSession

        sess = LinuxLiveSession(enable_audio=False)
        self.addCleanup(sess.close)
        rfd = os.open(sess.input_fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(lambda: os.close(rfd))
        win = LinuxGameWindow(sess)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.stop_polling)
        for kind, key in ((QEvent.KeyPress, Qt.Key_Z), (QEvent.KeyPress, Qt.Key_Space),
                          (QEvent.KeyPress, Qt.Key_Left), (QEvent.KeyRelease, Qt.Key_Z)):
            self.app.sendEvent(win, QKeyEvent(kind, key, Qt.NoModifier))
        raw = os.read(rfd, 24 * 4)
        self.assertEqual(struct.unpack_from('<Q', raw, 72)[0], DM_BTN_A | DM_BTN_LEFT)
        self.app.sendEvent(win, QFocusEvent(QEvent.FocusOut))
        self.assertEqual(struct.unpack_from('<Q', os.read(rfd, 24))[0], 0)
        self.app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_X, Qt.NoModifier))
        self.assertEqual(struct.unpack_from('<Q', os.read(rfd, 24))[0], 2)

    def test_audio_sink_stays_alive_when_started(self):
        from nspb_qt.linux_player import LinuxLiveSession, find_audio_player
        if not find_audio_player():
            self.skipTest('no pw-play or aplay installed')
        sess = LinuxLiveSession(enable_audio=True)
        self.addCleanup(sess.close)
        self.assertTrue(sess.audio_enabled)
        self.assertIsNotNone(sess._audio_proc)
        self.assertIsNone(sess._audio_proc.poll())


if __name__ == '__main__':
    unittest.main()
