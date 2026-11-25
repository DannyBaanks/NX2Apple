"""Live Linux game display window, shared-memory framebuffer reader, audio pipe and input bridge."""
from __future__ import annotations

import mmap
import os
import shutil
import struct
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Shared-memory framebuffer header ABI (matches engine/dm/host/linux/linux_gfx_stub.c).
FB_SHM_MAGIC = 0x4E584642  # "NXFB"
FB_SHM_VERSION = 1
FB_SHM_HDR_FMT = '<IIIIIIIIQQIIII'
FB_SHM_HDR_SZ = 64

# Switch NpadButton bitmask (matches engine/dm/host/dm_input.h).
DM_BTN_A = 1 << 0
DM_BTN_B = 1 << 1
DM_BTN_X = 1 << 2
DM_BTN_Y = 1 << 3
DM_BTN_LS = 1 << 4
DM_BTN_RS = 1 << 5
DM_BTN_L = 1 << 6
DM_BTN_R = 1 << 7
DM_BTN_ZL = 1 << 8
DM_BTN_ZR = 1 << 9
DM_BTN_PLUS = 1 << 10
DM_BTN_MINUS = 1 << 11
DM_BTN_LEFT = 1 << 12
DM_BTN_UP = 1 << 13
DM_BTN_RIGHT = 1 << 14
DM_BTN_DOWN = 1 << 15

KEY_MAP: dict[int, int] = {
    int(Qt.Key.Key_Up): DM_BTN_UP,
    int(Qt.Key.Key_W): DM_BTN_UP,
    int(Qt.Key.Key_Down): DM_BTN_DOWN,
    int(Qt.Key.Key_S): DM_BTN_DOWN,
    int(Qt.Key.Key_Left): DM_BTN_LEFT,
    int(Qt.Key.Key_A): DM_BTN_LEFT,
    int(Qt.Key.Key_Right): DM_BTN_RIGHT,
    int(Qt.Key.Key_D): DM_BTN_RIGHT,
    int(Qt.Key.Key_Z): DM_BTN_A,
    int(Qt.Key.Key_J): DM_BTN_A,
    int(Qt.Key.Key_Space): DM_BTN_A,
    int(Qt.Key.Key_X): DM_BTN_B,
    int(Qt.Key.Key_K): DM_BTN_B,
    int(Qt.Key.Key_Backspace): DM_BTN_B,
    int(Qt.Key.Key_C): DM_BTN_X,
    int(Qt.Key.Key_I): DM_BTN_X,
    int(Qt.Key.Key_V): DM_BTN_Y,
    int(Qt.Key.Key_U): DM_BTN_Y,
    int(Qt.Key.Key_Q): DM_BTN_L,
    int(Qt.Key.Key_E): DM_BTN_R,
    int(Qt.Key.Key_1): DM_BTN_ZL,
    int(Qt.Key.Key_2): DM_BTN_ZR,
    int(Qt.Key.Key_Return): DM_BTN_PLUS,
    int(Qt.Key.Key_Enter): DM_BTN_PLUS,
    int(Qt.Key.Key_Tab): DM_BTN_MINUS,
    int(Qt.Key.Key_Shift): DM_BTN_MINUS,
}


def pack_input(buttons: int, lx: int = 0, ly: int = 0, rx: int = 0, ry: int = 0) -> bytes:
    return struct.pack('<Qiiii', buttons & 0xFFFFFFFFFFFFFFFF, lx, ly, rx, ry)


class ShmFramebufferReader:
    """Zero-copy reader for the double-buffered NXFB shared-memory framebuffer."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._mm: mmap.mmap | None = None
        self._mapped_size = 0
        self.last_seq = 0
        self.width = 0
        self.height = 0
        self._fps_t0 = time.monotonic()
        self._fps_frames = 0
        self.fps = 0.0

    def _ensure_map(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size < FB_SHM_HDR_SZ:
            return False
        if self._mm is not None and self._mapped_size == st.st_size:
            return True
        self.close()
        try:
            self._fd = os.open(self.path, os.O_RDONLY)
            self._mm = mmap.mmap(self._fd, st.st_size, access=mmap.ACCESS_READ)
            self._mapped_size = st.st_size
            return True
        except OSError:
            self.close()
            return False

    def poll_image(self) -> QImage | None:
        """Return a fresh QImage if a new frame was presented since the last poll."""
        if not self._ensure_map() or self._mm is None:
            return None
        hdr = struct.unpack_from(FB_SHM_HDR_FMT, self._mm, 0)
        magic, ver, w, h, stride, fmt, active, buf_bytes, seq, _pns, cx, cy, cw, ch = hdr
        if magic != FB_SHM_MAGIC or ver != FB_SHM_VERSION or fmt != 1:
            return None
        if seq == 0 or seq == self.last_seq or w == 0 or h == 0 or stride < w * 4:
            return None
        slot = active & 1
        off = FB_SHM_HDR_SZ + slot * buf_bytes
        end = off + h * stride
        if end > self._mapped_size:
            return None
        raw = self._mm[off:end]
        img = QImage(raw, w, h, stride, QImage.Format.Format_RGBA8888).copy()
        if cw > 0 and ch > 0 and (cx != 0 or cy != 0 or cw != w or ch != h):
            if cx + cw <= w and cy + ch <= h:
                img = img.copy(cx, cy, cw, ch)
        self.last_seq = seq
        self.width, self.height = img.width(), img.height()
        self._fps_frames += 1
        now = time.monotonic()
        dt = now - self._fps_t0
        if dt >= 0.5:
            self.fps = self._fps_frames / dt
            self._fps_frames = 0
            self._fps_t0 = now
        return img

    def close(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except BufferError:
                pass
            self._mm = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._mapped_size = 0


def find_audio_player() -> list[str] | None:
    if pw := shutil.which('pw-play'):
        return [pw, '--format=s16', '--rate=48000', '--channels=2', '--latency=40ms']
    if aplay := shutil.which('aplay'):
        return [aplay, '-q', '-f', 'S16_LE', '-r', '48000', '-c', '2', '-t', 'raw']
    return None


class LinuxLiveSession:
    """Owns the temporary SHM framebuffer, input FIFO, and audio sink for one Linux game run."""

    def __init__(self, *, enable_audio: bool = True) -> None:
        shm_base = '/dev/shm' if Path('/dev/shm').is_dir() and os.access('/dev/shm', os.W_OK) else None
        self.dir = Path(tempfile.mkdtemp(prefix='nx2apple-live-', dir=shm_base))
        self.fb_shm = self.dir / 'fb.shm'
        self.input_fifo = self.dir / 'input.fifo'
        self.audio_fifo = self.dir / 'audio.fifo'
        os.mkfifo(self.input_fifo, 0o600)
        self._input_fd: int | None = None
        self._audio_proc: subprocess.Popen | None = None
        self._audio_reader_fd: int | None = None
        self.audio_enabled = False
        self.muted = False
        if enable_audio:
            self._start_audio_sink()
        self.reader = ShmFramebufferReader(self.fb_shm)

    def _start_audio_sink(self) -> None:
        cmd = find_audio_player()
        if not cmd:
            return
        os.mkfifo(self.audio_fifo, 0o600)
        try:
            # Open read+write so neither pw-play nor dmhost blocks if one starts first.
            rd = os.open(self.audio_fifo, os.O_RDWR | os.O_CLOEXEC)
            self._audio_reader_fd = rd
            self._audio_proc = subprocess.Popen(
                [*cmd, '-'],
                stdin=rd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.05)
            if self._audio_proc.poll() is not None:
                self._stop_audio_sink()
                return
            self.audio_enabled = True
        except OSError:
            self._stop_audio_sink()

    def _stop_audio_sink(self) -> None:
        if self._audio_proc is not None:
            try:
                self._audio_proc.terminate()
                self._audio_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._audio_proc.kill()
                except Exception:
                    pass
            self._audio_proc = None
        if self._audio_reader_fd is not None:
            try:
                os.close(self._audio_reader_fd)
            except OSError:
                pass
            self._audio_reader_fd = None
        self.audio_enabled = False

    def env_overrides(self) -> dict[str, str]:
        env = {
            'DM_FB_SHM': str(self.fb_shm),
            'DM_INPUT_PIPE': str(self.input_fifo),
        }
        if self.audio_enabled:
            env['DM_AUDIO'] = '1'
            env['DM_AUDIO_PIPE'] = str(self.audio_fifo)
        else:
            env['DM_AUDIO'] = '0'
        return env

    def send_input(self, buttons: int, lx: int = 0, ly: int = 0, rx: int = 0, ry: int = 0) -> bool:
        if self._input_fd is None:
            try:
                self._input_fd = os.open(self.input_fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                return False
        try:
            os.write(self._input_fd, pack_input(buttons, lx, ly, rx, ry))
            return True
        except BrokenPipeError:
            try:
                os.close(self._input_fd)
            except OSError:
                pass
            self._input_fd = None
            return False
        except OSError:
            return False

    def close(self) -> None:
        self.reader.close()
        if self._input_fd is not None:
            try:
                os.close(self._input_fd)
            except OSError:
                pass
            self._input_fd = None
        self._stop_audio_sink()
        shutil.rmtree(self.dir, ignore_errors=True)


class _GameCanvas(QWidget):
    """Aspect-ratio preserving canvas painted directly from the latest RGBA QImage."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self.smooth = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(640, 360)
        self.setStyleSheet('background: #08090b;')

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def current_image(self) -> QImage | None:
        return self._image

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is None or self._image.isNull():
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Waiting for first frame...')
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.smooth)
        iw, ih = self._image.width(), self._image.height()
        ww, wh = self.width(), self.height()
        if iw <= 0 or ih <= 0 or ww <= 0 or wh <= 0:
            return
        scale = min(ww / iw, wh / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        dx, dy = (ww - dw) // 2, (wh - dh) // 2
        p.drawImage(dx, dy, self._image.scaled(dw, dh, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation if self.smooth else Qt.TransformationMode.FastTransformation))


class LinuxGameWindow(QWidget):
    """Interactive live display window for a running Linux Switch host."""

    stop_requested = Signal()

    def __init__(self, session: LinuxLiveSession, title: str = 'NX2Apple Linux',
                 screenshots_dir: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.screenshots_dir = screenshots_dir
        self._buttons = 0
        self._held_keys: set[int] = set()
        self.setWindowTitle(f'{title} — NX2Apple Linux')
        self.resize(1120, 680)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            'QWidget { background: #101216; color: #e8eaed; }'
            'QPushButton { background: #1e222a; border: 1px solid #303642; border-radius: 6px; padding: 5px 12px; }'
            'QPushButton:hover { background: #2a303b; }'
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = _GameCanvas(self)
        root.addWidget(self.canvas, 1)

        hud = QWidget(self)
        hud.setStyleSheet('background: #14171d; border-top: 1px solid #262b35;')
        row = QHBoxLayout(hud)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)

        self.info_label = QLabel('0 fps · Waiting for frames')
        self.info_label.setStyleSheet('color: #9aa4b2; font-size: 12px;')
        row.addWidget(self.info_label)

        self.keys_label = QLabel('Arrows/WASD: D-Pad · Z/Space: A · X/Bksp: B · Enter: + · Tab: - · Q/E: L/R')
        self.keys_label.setStyleSheet('color: #7b8494; font-size: 11px;')
        row.addWidget(self.keys_label, 1)

        self.shot_btn = QPushButton('Screenshot')
        self.shot_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.shot_btn.clicked.connect(self.save_screenshot)
        row.addWidget(self.shot_btn)

        self.stop_btn = QPushButton('Stop')
        self.stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.stop_btn)

        root.addWidget(hud)

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        img = self.session.reader.poll_image()
        if img is not None:
            self.canvas.set_image(img)
            r = self.session.reader
            audio_tag = 'Audio ON' if self.session.audio_enabled else 'Audio OFF'
            self.info_label.setText(f'{r.fps:4.1f} fps · {r.width}x{r.height} · Frame #{r.last_seq} · {audio_tag}')

    def save_screenshot(self) -> Path | None:
        img = self.canvas.current_image()
        if img is None or img.isNull():
            return None
        out_dir = self.screenshots_dir or (Path.home() / 'Pictures')
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f'nx2apple-{datetime.now().strftime("%Y%m%d-%H%M%S")}.png'
        if img.save(str(out)):
            self.info_label.setText(f'Saved: {out.name}')
            return out
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        bit = KEY_MAP.get(int(event.key()))
        if bit:
            self._held_keys.add(int(event.key()))
            self._update_buttons()
            self.session.send_input(self._buttons)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        bit = KEY_MAP.get(int(event.key()))
        if bit:
            self._held_keys.discard(int(event.key()))
            self._update_buttons()
            self.session.send_input(self._buttons)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _update_buttons(self) -> None:
        self._buttons = 0
        for key in self._held_keys:
            self._buttons |= KEY_MAP[key]

    def focusOutEvent(self, event) -> None:
        self._held_keys.clear()
        if self._buttons:
            self._buttons = 0
            self.session.send_input(0)
        super().focusOutEvent(event)

    def stop_polling(self) -> None:
        self._timer.stop()

    def closeEvent(self, event) -> None:
        self._held_keys.clear()
        self._timer.stop()
        if self._buttons:
            self._buttons = 0
            self.session.send_input(0)
        self.stop_requested.emit()
        super().closeEvent(event)
