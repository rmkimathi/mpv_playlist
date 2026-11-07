#!.venv/bin/python3
r"""
MPV Playlist - a PySide6 (Qt) remote control for the local mpv player.

Features
  * Playlist  : add files / folder / URL with native file dialogs, drag & drop (files,
                folders, .m3u, URLs, subtitles - from the file manager or the browser),
                drag to reorder, right-click menu, remove, shuffle, loop, load/save .m3u
  * Settings  : edit the global part of mpv.conf (~/.config/mpv on Linux, %APPDATA%\mpv
                on Windows): hwdec, geometry, screenshot dir, ytdl-format, subtitle
                options; apply live and/or save
  * Equalizer : 10-band equalizer using the same bands/format as your mpv.conf,
                applied live through a labelled lavfi filter (@eq)

Setup (Linux / Ubuntu)
  python3 -m venv .venv && source .venv/bin/activate
  pip install PySide6
  sudo apt install mpv libxcb-cursor0      (libxcb-cursor0 is needed by Qt on X11)
  python3 mpv_playlist_qt.py

Setup (Windows 10/11)
  Install Python 3 and mpv (for example "scoop install mpv"), or simply put mpv.exe
  next to this script. For YouTube/URLs also put yt-dlp.exe next to mpv.exe or on PATH.
    py -m venv .venv
    .venv\Scripts\activate
    pip install PySide6
    python mpv_playlist_qt.py

Shortcuts
  Ctrl+O add files | Ctrl+Shift+O add folder | Ctrl+U add URL | Ctrl+V paste paths/URLs
  Enter play selected | Delete remove selected | Ctrl+A select all
  Double-click an equalizer slider to reset it to 0 dB.

How it works
  The script starts its own mpv (--idle=yes --force-window=yes) and talks to it over
  mpv's JSON IPC (a unix socket on Linux, a named pipe on Windows). Your mpv.conf is
  still loaded by mpv as usual; the GUI only overrides things at runtime when you ask.
  The app's own state (equalizer, volume, window size, last folder) is saved in
  state.json next to this script.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QItemSelectionModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QDesktopServices, QFont, QGuiApplication, QIcon,
                           QKeySequence, QPainter, QPalette, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QFileDialog, QFontComboBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy,
                               QSlider, QSpinBox, QStyle, QTabWidget, QVBoxLayout, QWidget)

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
IS_WINDOWS = sys.platform == "win32"

# Folder of this script (or of the .exe when frozen with PyInstaller).
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
#APP_STATE = APP_DIR / "state.json"
#APP_ICON = APP_DIR / "mpvl.ico"

IS_FROZEN = getattr(sys, "frozen", False)
EXE_DIR = Path(sys.executable if IS_FROZEN else __file__).resolve().parent
ASSET_DIR = Path(sys._MEIPASS).resolve() if IS_FROZEN else Path(__file__).resolve().parent
APP_STATE = EXE_DIR / "state.json"
APP_ICON = ASSET_DIR / "mpvl.ico"

FONT_MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"
INSTALL_HINT = ("install mpv and add it to PATH, or put mpv.exe next to this script"
                if IS_WINDOWS else "sudo apt install mpv")


def find_mpv():
    """mpv next to this script wins, then PATH, then a few usual Windows locations."""
    names = ("mpv.exe", "mpv.com") if IS_WINDOWS else ("mpv",)
    for n in names:
        if (APP_DIR / n).is_file():
            return str(APP_DIR / n)
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    if IS_WINDOWS:
        for root in (Path.home() / "scoop" / "apps" / "mpv" / "current",
                     Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "mpv"):
            if (root / "mpv.exe").is_file():
                return str(root / "mpv.exe")
    return None


MPV_EXE = find_mpv()


def mpv_config_dir():
    if MPV_EXE and (Path(MPV_EXE).parent / "portable_config").is_dir():
        return Path(MPV_EXE).parent / "portable_config"     # mpv's portable mode
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "mpv"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mpv"


MPV_CONF = mpv_config_dir() / "mpv.conf"

MEDIA_EXT = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts", ".flv", ".wmv",
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".alac", ".ape",
}
PLAYLIST_EXT = {".m3u", ".m3u8"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
MEDIA_FILTER = ("Media files (" + " ".join(f"*{e}" for e in sorted(MEDIA_EXT)) + ");;"
                "All files (*)")

EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_PRESETS = {
    "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "My mpv.conf curve": [0, 0, 0, 0.1, 0.3, 0.5, 0.9, 1.9, 5.9, 12],
    "Bass boost": [6, 5, 4, 2, 1, 0, 0, 0, 0, 0],
    "Treble boost": [0, 0, 0, 0, 0, 0, 2, 4, 6, 12],
    "Vocal": [-2, -2, -1, 1, 3, 4, 3, 1, 0, -1],
    "Loudness": [5, 3, 0, 0, -1, -1, 0, 1, 3, 4],
    "Rock": [4, 3, 2, 0, -1, -1, 0, 2, 3, 4],
}

# (mpv option, label, quote character used when writing to mpv.conf)
SETTINGS = [
    ("hwdec", "Hardware decoding (hwdec)", ""),
    ("hwdec-codecs", "hwdec codecs", ""),
    ("geometry", "Window geometry", ""),
    ("screenshot-directory", "Screenshot directory", '"'),
    ("ytdl-format", "yt-dlp format", "'"),
    ("sub-auto", "Subtitle auto-load", ""),
    ("sub-font", "Subtitle font", "'"),
    ("sub-font-size", "Subtitle font size", ""),
]
SETTING_DEFAULTS = {
    "hwdec": "auto-safe" if IS_WINDOWS else "vaapi",
    "hwdec-codecs": "mpeg2video,h264,vc1,hevc,vp8,vp9,av1",
    "geometry": "1280x720",
    "screenshot-directory": (Path.home() / "Pictures" / "MPV").as_posix() if IS_WINDOWS
                            else "~/Pictures/MPV",
    "ytdl-format": "bestvideo[height<=?720][fps<=?30]+bestaudio/best",
    "sub-auto": "all",
    "sub-font": "Aptos Black",
    "sub-font-size": "40",
}
HWDEC_CHOICES = (["no", "auto", "auto-safe", "d3d11va", "d3d11va-copy", "dxva2", "nvdec", "nvdec-copy"]
                 if IS_WINDOWS else
                 ["no", "auto", "auto-safe", "vaapi", "vaapi-copy", "nvdec", "vulkan"])


# --------------------------------------------------------------------------- #
# mpv JSON IPC client
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _PeekNamedPipe = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    _PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
    _PeekNamedPipe.restype = wintypes.BOOL


class MpvIPC:
    OBSERVED = ["time-pos", "duration", "pause", "volume", "mute", "speed",
                "media-title", "playlist", "idle-active"]

    def __init__(self):
        if IS_WINDOWS:
            self.ipc_path = rf"\\.\pipe\mpv-playlist-{os.getpid()}"      # named pipe
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            self.ipc_path = os.path.join(runtime, f"mpv-playlist-{os.getpid()}.sock")
        self.proc = None
        self.conn = None
        self.state = {}
        self._alive = False
        self._lock = threading.Lock()
        self._req = 0
        self._pending = {}

    # -- lifecycle --------------------------------------------------------- #
    def alive(self):
        return self._alive and self.proc is not None and self.proc.poll() is None

    def _open(self):
        """One attempt to connect to mpv's IPC endpoint; returns a connection or None."""
        if IS_WINDOWS:
            try:
                return open(self.ipc_path, "r+b", buffering=0)
            except OSError:
                return None
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.ipc_path)
            return s
        except OSError:
            s.close()
            return None

    def start(self):
        if not MPV_EXE:
            raise RuntimeError(f"mpv was not found ({INSTALL_HINT})")
        self._cleanup_socket()
        self.state = {}
        self.proc = subprocess.Popen(
            [MPV_EXE, "--idle=yes", "--force-window=yes", "--no-terminal",
             f"--input-ipc-server={self.ipc_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        conn = None
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("mpv exited right after starting")
            conn = self._open()
            if conn is not None:
                break
            time.sleep(0.1)
        if conn is None:
            raise RuntimeError("Could not connect to mpv's IPC endpoint")
        self.conn = conn
        self._alive = True
        threading.Thread(target=self._reader, args=(conn,), daemon=True).start()
        for i, name in enumerate(self.OBSERVED, 1):
            self.command("observe_property", 100 + i, name)

    def quit(self):
        try:
            if self.alive():
                self.command("quit")
                self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self._alive = False
        conn, self.conn = self.conn, None
        try:
            if conn is not None:
                conn.close()
        except OSError:
            pass
        self._cleanup_socket()

    def _cleanup_socket(self):
        if IS_WINDOWS:
            return                      # named pipes vanish with the process
        try:
            os.remove(self.ipc_path)
        except OSError:
            pass

    # -- IO ---------------------------------------------------------------- #
    def _recv(self, conn):
        """Wait for data from mpv. Returns b"" when the connection is gone."""
        if not IS_WINDOWS:
            return conn.recv(65536)
        # A blocking read on a synchronous Windows pipe handle would also block writes
        # from other threads, so only read once PeekNamedPipe reports waiting data.
        handle = msvcrt.get_osfhandle(conn.fileno())
        avail = wintypes.DWORD(0)
        while conn is self.conn:
            if not _PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
                return b""
            if avail.value:
                return conn.read(avail.value)
            time.sleep(0.02)
        return b""

    def _send(self, data):
        if IS_WINDOWS:
            self.conn.write(data)
        else:
            self.conn.sendall(data)

    def _reader(self, conn):
        buf = b""
        try:
            while True:
                data = self._recv(conn)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            if conn is self.conn:
                self._alive = False

    def _dispatch(self, msg):
        rid = msg.get("request_id")
        if rid is not None and rid in self._pending:
            slot = self._pending[rid]
            slot[1] = msg
            slot[0].set()
        elif msg.get("event") == "property-change":
            self.state[msg.get("name")] = msg.get("data")

    def command(self, *args, wait=False, timeout=2.0):
        if self.conn is None:
            return None
        with self._lock:
            self._req += 1
            rid = self._req
            if wait:
                self._pending[rid] = [threading.Event(), None]
            payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
            try:
                self._send(payload.encode())
            except (OSError, ValueError, AttributeError):
                self._alive = False
                self._pending.pop(rid, None)
                return None
        if wait:
            slot = self._pending[rid]
            slot[0].wait(timeout)
            self._pending.pop(rid, None)
            return slot[1]
        return None

    def set(self, name, value):
        self.command("set_property", name, value)


# --------------------------------------------------------------------------- #
# mpv.conf helpers (only touch the global section, keep everything else)
# --------------------------------------------------------------------------- #
def _strip_quotes(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_global_conf():
    result = {}
    if not MPV_CONF.exists():
        return result
    for line in MPV_CONF.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            break
        if "=" in s:
            k, v = s.split("=", 1)
            result[k.strip()] = _strip_quotes(v)
    return result


def write_global_conf(updates):
    """updates: {option: value}. Empty value removes the option."""
    quote = {k: q for k, _, q in SETTINGS}
    lines = []
    if MPV_CONF.exists():
        lines = MPV_CONF.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        shutil.copy2(MPV_CONF, str(MPV_CONF) + ".bak")
    first_section = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))

    def fmt(k, v):
        q = quote.get(k, "")
        return f"{k}={q}{v}{q}"

    out, done = [], set()
    for i, line in enumerate(lines):
        key = None
        s = line.strip()
        if i < first_section and s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
        if key in updates:
            done.add(key)
            if updates[key] != "":
                out.append(fmt(key, updates[key]))
            continue
        out.append(line)

    missing = [k for k, v in updates.items() if k not in done and v != ""]
    if missing:
        # insert after the last non-empty line of the global section
        end_global = min(first_section, len(out))
        insert_at = 0
        for i in range(end_global):
            if out[i].strip():
                insert_at = i + 1
        out[insert_at:insert_at] = [fmt(k, updates[k]) for k in missing]

    MPV_CONF.parent.mkdir(parents=True, exist_ok=True)
    MPV_CONF.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Misc helpers (no Qt in here)
# --------------------------------------------------------------------------- #
def fmt_time(sec):
    if sec is None:
        return "--:--"
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_url(s):
    return bool(re.match(r"^[a-zA-Z][\w+.-]+://", s))


def display_name(entry):
    name = entry.get("title") or entry.get("filename", "")
    return name if is_url(name) else os.path.basename(name)


def eq_graph(gains):
    parts = [f"equalizer=frequency={f:g}:width_type=o:width=1:gain={g:g}"
             for f, g in zip(EQ_BANDS, gains) if abs(g) > 1e-9]
    return ",".join(parts)


def load_app_state():
    try:
        return json.loads(APP_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_app_state(data):
    try:
        APP_STATE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_m3u(path):
    base = os.path.dirname(path)
    entries = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line if is_url(line) or os.path.isabs(line) else os.path.join(base, line))
    return entries


def scan_folder(folder, recursive=False):
    """Media files in a folder, naturally sorted (10 after 9), optionally with subfolders."""
    found = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort(key=natural_key)
            found += [os.path.join(root, f) for f in sorted(files, key=natural_key)
                      if Path(f).suffix.lower() in MEDIA_EXT]
    else:
        found = [os.path.join(folder, f) for f in sorted(os.listdir(folder), key=natural_key)
                 if Path(f).suffix.lower() in MEDIA_EXT and os.path.isfile(os.path.join(folder, f))]
    return found


def expand_inputs(paths, recursive=True):
    """Turn dropped/pasted paths into (media entries, subtitle files):
    folders are scanned, .m3u playlists are read, URLs pass through."""
    media, subs = [], []
    for p in paths:
        ext = Path(p).suffix.lower()
        if is_url(p):
            media.append(p)
        elif os.path.isdir(p):
            media += scan_folder(p, recursive)
        elif not os.path.exists(p):
            continue
        elif ext in PLAYLIST_EXT:
            media += read_m3u(p)
        elif ext in SUB_EXT:
            subs.append(p)
        else:
            media.append(p)
    return media, subs


def mime_paths(mime):
    """File paths / URLs carried by a drag-and-drop or clipboard QMimeData."""
    out = []
    if mime.hasUrls():
        for u in mime.urls():
            out.append(os.path.normpath(u.toLocalFile()) if u.isLocalFile() else u.toString())
    elif mime.hasText():
        for line in mime.text().splitlines():
            line = line.strip().strip('"')
            if line and (is_url(line) or os.path.exists(line)):
                out.append(line)
    return out


def plan_moves(count, sources, target):
    """Work out the mpv `playlist-move` commands that put the entries `sources` (indices)
    in one block just before index `target` (`count` = the very end), keeping their order.
    mpv semantics: `playlist-move i j` inserts entry i before the entry that is currently
    at index j. Returns (commands, final_order): commands are (i, j) pairs and
    final_order lists the original indices in their new order."""
    order = list(range(count))
    moving = sorted(set(sources))
    anchor = next((x for x in order[target:] if x not in moving), None)
    cmds = []
    for s in moving:
        i = order.index(s)
        j = order.index(anchor) if anchor is not None else len(order)
        if i + 1 != j:
            cmds.append((i, j))
            order.pop(i)
            order.insert(order.index(anchor) if anchor is not None else len(order), s)
    if order == list(range(count)):
        cmds = []
    return cmds, order


def shift_plan(count, selected, direction):
    """Commands to move every selected row one step up (-1) or down (+1), keeping gaps.
    Returns (commands, new_selection); rows already at the edge stay put."""
    cmds, new = [], []
    if direction < 0:
        blocked = 0
        for s in sorted(selected):
            if s == blocked:
                blocked += 1
                new.append(s)
            else:
                cmds.append((s, s - 1))
                new.append(s - 1)
    else:
        limit = count - 1
        for s in sorted(selected, reverse=True):
            if s == limit:
                limit -= 1
                new.append(s)
            else:
                cmds.append((s, s + 2))
                new.append(s + 1)
    return cmds, sorted(new)


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
def accept_as_copy(event):
    """Accept a drop as a *copy*. Accepting the proposed "move" could make a file manager
    delete the original files after we only wanted to read their paths."""
    event.setDropAction(Qt.DropAction.CopyAction)
    event.accept()


class SeekSlider(QSlider):
    """Horizontal slider where a click jumps straight to the clicked position."""

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(0, 1000)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _value_at(self, ev):
        span = max(self.width() - 1, 1)
        x = min(max(int(ev.position().x()), 0), span)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._value_at(ev))
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self.isSliderDown():
            self.setValue(self._value_at(ev))
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.isSliderDown() and ev.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._value_at(ev))
            self.setSliderDown(False)        # emits sliderReleased -> seek
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)


class EqSlider(QSlider):
    """Vertical gain slider in 0.1 dB steps (+12 dB on top); double-click resets to 0."""

    def __init__(self):
        super().__init__(Qt.Orientation.Vertical)
        self.setRange(-120, 120)
        self.setPageStep(10)
        self.setTickPosition(QSlider.TickPosition.TicksBothSides)
        self.setTickInterval(60)

    def mouseDoubleClickEvent(self, ev):
        self.setValue(0)
        ev.accept()


class PlaylistWidget(QListWidget):
    """Playlist list with drag & drop in both directions and a few key shortcuts.

    Nothing is moved by Qt itself: drops are reported through signals and the owner
    sends the matching commands to mpv, so the list always mirrors mpv's playlist."""

    pathsDropped = Signal(list, int)    # dropped paths/URLs, row to insert before
    rowsMoved = Signal(list, int)       # dragged rows, row to insert before
    playRequested = Signal()
    removeRequested = Signal()
    pasteRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)           # we paint our own, it also works for URLs
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dragging = False
        self._drop_row = None

    # -- drag out (reordering) -------------------------------------------- #
    def startDrag(self, supported_actions):
        self.dragging = True
        try:
            super().startDrag(supported_actions)
        finally:
            self.dragging = False

    # -- drag in (reordering + files/URLs from outside) --------------------- #
    def _usable(self, event):
        md = event.mimeData()
        return event.source() is self or md.hasUrls() or md.hasText()

    def _row_at(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        below = pos.y() > self.visualItemRect(item).center().y()
        return self.row(item) + (1 if below else 0)

    def _autoscroll(self, pos):
        bar = self.verticalScrollBar()
        if pos.y() < 24:
            bar.setValue(bar.value() - bar.singleStep())
        elif pos.y() > self.viewport().height() - 24:
            bar.setValue(bar.value() + bar.singleStep())

    def dragEnterEvent(self, event):
        if self._usable(event):
            accept_as_copy(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._usable(event):
            event.ignore()
            return
        pos = event.position().toPoint()
        self._drop_row = self._row_at(pos)
        self._autoscroll(pos)
        self.viewport().update()
        accept_as_copy(event)

    def dragLeaveEvent(self, event):
        self._drop_row = None
        self.viewport().update()
        event.accept()

    def dropEvent(self, event):
        row = self._row_at(event.position().toPoint())
        self._drop_row = None
        self.viewport().update()
        if not self._usable(event):
            event.ignore()
            return
        if event.source() is self:
            self.rowsMoved.emit(sorted(self.row(i) for i in self.selectedItems()), row)
        else:
            self.pathsDropped.emit(mime_paths(event.mimeData()), row)
        accept_as_copy(event)

    # -- painting: empty-state hint + drop position line ---------------------- #
    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self.viewport())
        if self.count() == 0:
            p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                       "Drop files, folders or URLs here\n(or right-click for options)")
        if self._drop_row is not None:
            if self.count() == 0:
                y = 0
            elif self._drop_row < self.count():
                y = self.visualItemRect(self.item(self._drop_row)).top()
            else:
                y = self.visualItemRect(self.item(self.count() - 1)).bottom()
            p.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 2))
            p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    # -- keys ---------------------------------------------------------------- #
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pasteRequested.emit()
        elif event.key() == Qt.Key.Key_Delete:
            self.removeRequested.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.playRequested.emit()
        else:
            super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MPV Playlist")
        self.mpv = MpvIPC()
        self.saved = load_app_state()
        gains = self.saved.get("eq_gains")
        if not (isinstance(gains, list) and len(gains) == len(EQ_BANDS)):
            gains = EQ_PRESETS["Flat"]
        self.eq_gains = [float(x) for x in gains]
        self.eq_enabled = bool(self.saved.get("eq_enabled", False))
        self.volume = float(self.saved.get("volume", 100))
        self.last_dir = self.saved.get("last_dir") or str(Path.home())
        self.last_playlist = []
        self.playlist_sig = None
        self.pending_select = None
        self.current_index = None
        self.vol_touch = 0.0
        self.dead_notified = False

        self.eq_timer = QTimer(self)             # debounce: apply EQ shortly after the last change
        self.eq_timer.setSingleShot(True)
        self.eq_timer.setInterval(150)
        self.eq_timer.timeout.connect(self.apply_eq)

        self.setAcceptDrops(True)                # drop files anywhere on the window
        self.build_ui()
        self.build_menu()
        self.resize(900, 650)
        try:
            if self.saved.get("window"):
                self.restoreGeometry(QByteArray.fromBase64(self.saved["window"].encode("ascii")))
        except Exception:
            pass

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh_ui)
        self.timer.start()
        QTimer.singleShot(0, self.start_mpv_initial)

    # -- UI construction ------------------------------------------------------ #
    def icon(self, name):
        return self.style().standardIcon(getattr(QStyle.StandardPixmap, name))

    def button(self, text, slot, icon=None):
        b = QPushButton(text)
        if icon:
            b.setIcon(self.icon(icon))
        b.clicked.connect(lambda *_: slot())     # clicked(bool) -> slot() without arguments
        return b

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.addWidget(self.build_player())
        tabs = QTabWidget()
        tabs.addTab(self.build_playlist_tab(), "Playlist")
        tabs.addTab(self.build_eq_tab(), "Equalizer")
        tabs.addTab(self.build_settings_tab(), "Settings")
        root.addWidget(tabs, 1)
        self.statusBar().showMessage("Starting...")

    def build_player(self):
        box = QGroupBox("Player")
        v = QVBoxLayout(box)

        self.now_label = QLabel("Idle")
        font = self.now_label.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        self.now_label.setFont(font)
        self.now_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        v.addWidget(self.now_label)

        row = QHBoxLayout()
        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setMinimumWidth(self.fontMetrics().horizontalAdvance("0:00:00 / 0:00:00") + 8)
        self.seek = SeekSlider()
        self.seek.sliderReleased.connect(self.on_seek_released)
        self.seek.valueChanged.connect(self.on_seek_changed)
        row.addWidget(self.time_label)
        row.addWidget(self.seek, 1)
        v.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(self.button("Prev", lambda: self.cmd("playlist-prev"), "SP_MediaSkipBackward"))
        self.play_btn = self.button("Play", self.on_play, "SP_MediaPlay")
        row.addWidget(self.play_btn)
        row.addWidget(self.button("Stop", lambda: self.cmd("stop"), "SP_MediaStop"))
        row.addWidget(self.button("Next", lambda: self.cmd("playlist-next"), "SP_MediaSkipForward"))
        row.addSpacing(12)
        self.mute_btn = self.button("Mute", lambda: self.cmd("cycle", "mute"), "SP_MediaVolume")
        row.addWidget(self.mute_btn)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 130)
        self.vol_slider.setValue(int(self.volume))
        self.vol_slider.setFixedWidth(140)
        self.vol_slider.setToolTip("Volume")
        self.vol_slider.valueChanged.connect(self.on_volume)
        row.addWidget(self.vol_slider)
        row.addSpacing(12)
        row.addWidget(QLabel("Speed"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5", "0.75", "1.0", "1.25", "1.5", "2.0"])
        self.speed_combo.setCurrentText("1.0")
        self.speed_combo.activated.connect(
            lambda *_: self.cmd("set_property", "speed", float(self.speed_combo.currentText())))
        row.addWidget(self.speed_combo)
        row.addStretch(1)
        v.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(self.button("Fullscreen", lambda: self.cmd("cycle", "fullscreen")))
        row.addWidget(self.button("Subtitles", lambda: self.cmd("cycle", "sub")))
        row.addWidget(self.button("Audio track", lambda: self.cmd("cycle", "audio")))
        row.addWidget(self.button("Screenshot", self.on_screenshot))
        row.addStretch(1)
        v.addLayout(row)
        return box

    def build_playlist_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.list = PlaylistWidget()
        self.list.setFont(QFont(FONT_MONO, 10))
        self.list.itemDoubleClicked.connect(lambda item: self.cmd("playlist-play-index", self.list.row(item)))
        self.list.rowsMoved.connect(self.move_rows)
        self.list.pathsDropped.connect(self.handle_drop)
        self.list.playRequested.connect(self.play_selected)
        self.list.removeRequested.connect(self.remove_selected)
        self.list.pasteRequested.connect(self.paste_clipboard)
        self.list.customContextMenuRequested.connect(self.show_playlist_menu)
        v.addWidget(self.list, 1)

        row = QHBoxLayout()
        row.addWidget(self.button("Add files", self.add_files_dialog))
        row.addWidget(self.button("Add folder", lambda: self.add_folder_dialog(False)))
        row.addWidget(self.button("Add URL", self.add_url_dialog))
        row.addWidget(self.button("Remove", self.remove_selected))
        row.addWidget(self.button("Up", lambda: self.shift_rows(-1)))
        row.addWidget(self.button("Down", lambda: self.shift_rows(1)))
        row.addWidget(self.button("Clear", self.clear_playlist))
        row.addWidget(self.button("Shuffle", lambda: self.cmd("playlist-shuffle")))
        row.addStretch(1)
        v.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(self.button("Play selected", self.play_selected))
        row.addWidget(self.button("Load .m3u", self.load_m3u))
        row.addWidget(self.button("Save .m3u", self.save_m3u))
        row.addSpacing(12)
        row.addWidget(QLabel("Repeat:"))
        self.loop_combo = QComboBox()
        self.loop_combo.addItems(["Off", "Playlist", "Current file"])
        self.loop_combo.activated.connect(self.on_loop)
        row.addWidget(self.loop_combo)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def build_eq_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        row = QHBoxLayout()
        self.eq_check = QCheckBox("Enable equalizer")
        self.eq_check.setChecked(self.eq_enabled)
        self.eq_check.toggled.connect(self.on_eq_toggle)
        row.addWidget(self.eq_check)
        row.addSpacing(12)
        row.addWidget(QLabel("Preset:"))
        self.eq_preset = QComboBox()
        self.eq_preset.addItems(list(EQ_PRESETS))
        self.eq_preset.setCurrentIndex(-1)
        self.eq_preset.activated.connect(self.on_eq_preset)
        row.addWidget(self.eq_preset)
        row.addWidget(self.button("Reset", self.on_eq_reset))
        row.addWidget(self.button("Copy mpv.conf lines", self.on_eq_copy))
        row.addStretch(1)
        v.addLayout(row)

        hint = QLabel("Gain in dB (+12 top, -12 bottom). Bands in Hz. Double-click a slider to reset it.")
        hint.setEnabled(False)
        v.addWidget(hint)

        cols = QHBoxLayout()
        self.eq_sliders, self.eq_value_labels = [], []
        for i, label in enumerate(EQ_LABELS):
            col = QVBoxLayout()
            value = QLabel()
            value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            slider = EqSlider()
            slider.setValue(round(self.eq_gains[i] * 10))
            value.setText(f"{self.eq_gains[i]:+.1f}")
            slider.valueChanged.connect(lambda val, i=i: self.on_eq_slider(i, val))
            name = QLabel(label)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(value)
            col.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(name)
            cols.addLayout(col)
            self.eq_sliders.append(slider)
            self.eq_value_labels.append(value)
        v.addLayout(cols, 1)
        return w

    def build_settings_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        conf = dict(SETTING_DEFAULTS)
        conf.update(read_global_conf())

        form = QFormLayout()
        self.sw = {}
        for key, label, _ in SETTINGS:
            if key == "hwdec":
                field = QComboBox()
                field.setEditable(True)
                field.addItems(HWDEC_CHOICES)
                field.setCurrentText(conf[key])
            elif key == "sub-auto":
                field = QComboBox()
                field.addItems(["no", "exact", "fuzzy", "all"])
                field.setCurrentText(conf[key])
            elif key == "sub-font":
                field = QFontComboBox()          # lists installed fonts, still accepts any name
                field.setEditable(True)
                field.setCurrentText(conf[key])
            elif key == "sub-font-size":
                field = QSpinBox()
                field.setRange(10, 160)
                try:
                    field.setValue(int(conf[key]))
                except ValueError:
                    field.setValue(int(SETTING_DEFAULTS[key]))
            else:
                field = QLineEdit(conf[key])
            self.sw[key] = field
            if key == "screenshot-directory":
                holder = QHBoxLayout()
                holder.addWidget(field, 1)
                holder.addWidget(self.button("Browse…", self.browse_screenshot_dir))
                form.addRow(label + ":", holder)
            else:
                form.addRow(label + ":", field)
        v.addLayout(form)

        row = QHBoxLayout()
        row.addWidget(self.button("Apply to running mpv", self.on_settings_apply))
        row.addWidget(self.button("Save to mpv.conf", self.on_settings_save))
        row.addStretch(1)
        v.addLayout(row)

        info = QLabel(f"Config file: {MPV_CONF}\n\n"
                      "Saving only edits the global section above the first [profile] and keeps "
                      "a backup (mpv.conf.bak).\nGeometry only takes effect when mpv starts; "
                      "hwdec applies to the next file loaded.")
        info.setWordWrap(True)
        info.setEnabled(False)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(info)
        v.addStretch(1)
        return w

    def build_menu(self):
        def action(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(lambda *_: slot())
            if shortcut is not None:
                a.setShortcut(QKeySequence(shortcut))
            menu.addAction(a)

        m = self.menuBar().addMenu("&File")
        action(m, "Add &files…", self.add_files_dialog, QKeySequence.StandardKey.Open)
        action(m, "Add f&older…", lambda: self.add_folder_dialog(False), "Ctrl+Shift+O")
        action(m, "Add folder with &subfolders…", lambda: self.add_folder_dialog(True))
        action(m, "Add &URL…", self.add_url_dialog, "Ctrl+U")
        m.addSeparator()
        action(m, "&Open playlist…", self.load_m3u)
        action(m, "&Save playlist…", self.save_m3u, QKeySequence.StandardKey.Save)
        m.addSeparator()
        action(m, "E&xit", self.close, QKeySequence.StandardKey.Quit)

    # -- mpv plumbing ------------------------------------------------------------ #
    def status(self, msg):
        self.statusBar().showMessage(msg)

    def start_mpv_initial(self):
        try:
            self.start_mpv()
        except RuntimeError as e:
            self.status(str(e))
            QMessageBox.critical(self, "mpv", str(e))

    def start_mpv(self):
        restart = self.dead_notified or bool(self.last_playlist)
        self.mpv.start()
        self.dead_notified = False
        self.mpv.set("volume", self.volume)
        self.apply_eq()
        if restart:
            for f in self.last_playlist:
                self.mpv.command("loadfile", f, "append")
        self.status("mpv started")

    def ensure_mpv(self):
        if self.mpv.alive():
            return True
        try:
            self.start_mpv()
            return True
        except RuntimeError as e:
            self.status(str(e))
            return False

    def cmd(self, *args):
        if self.ensure_mpv():
            self.mpv.command(*args)

    # -- transport ----------------------------------------------------------------- #
    def on_play(self):
        st = self.mpv.state
        if st.get("idle-active") and (st.get("playlist") or []):
            self.cmd("playlist-play-index", 0)
        else:
            self.cmd("cycle", "pause")

    def on_volume(self, value):
        self.volume = float(value)
        self.vol_touch = time.time()
        self.cmd("set_property", "volume", self.volume)

    def on_seek_changed(self, value):
        if self.seek.isSliderDown():             # live time preview while dragging
            dur = self.mpv.state.get("duration")
            if dur:
                self.time_label.setText(f"{fmt_time(value / 1000 * dur)} / {fmt_time(dur)}")

    def on_seek_released(self):
        dur = self.mpv.state.get("duration")
        if dur:
            self.cmd("seek", self.seek.value() / 1000 * dur, "absolute")

    def on_loop(self, *_):
        mode = self.loop_combo.currentText()
        self.cmd("set_property", "loop-playlist", "inf" if mode == "Playlist" else "no")
        self.cmd("set_property", "loop-file", "inf" if mode == "Current file" else "no")

    def on_screenshot(self):
        d = os.path.expanduser(self.sw["screenshot-directory"].text().strip())
        if d:
            os.makedirs(d, exist_ok=True)
        self.cmd("screenshot")
        self.status("Screenshot requested")

    # -- equalizer ------------------------------------------------------------------ #
    def apply_eq(self):
        self.mpv.command("af", "remove", "@eq")
        graph = eq_graph(self.eq_gains)
        if self.eq_enabled and graph:
            self.mpv.command("af", "add", f"@eq:lavfi=[{graph}]")

    def on_eq_toggle(self, checked):
        self.eq_enabled = bool(checked)
        self.apply_eq()

    def on_eq_slider(self, i, value):
        self.eq_gains[i] = value / 10
        self.eq_value_labels[i].setText(f"{self.eq_gains[i]:+.1f}")
        self.eq_timer.start()

    def set_eq_sliders(self, gains):
        for i, g in enumerate(gains):
            self.eq_sliders[i].blockSignals(True)
            self.eq_sliders[i].setValue(round(float(g) * 10))
            self.eq_sliders[i].blockSignals(False)
            self.eq_gains[i] = float(g)
            self.eq_value_labels[i].setText(f"{self.eq_gains[i]:+.1f}")
        self.eq_timer.start()

    def on_eq_preset(self, *_):
        self.set_eq_sliders(EQ_PRESETS[self.eq_preset.currentText()])

    def on_eq_reset(self):
        self.set_eq_sliders(EQ_PRESETS["Flat"])
        self.eq_preset.setCurrentText("Flat")

    def eq_conf_lines(self):
        out = ["# AUDIO EQUALIZER (10 band): ON"]
        for f, g in zip(EQ_BANDS, self.eq_gains):
            out.append(f"af-add=lavfi=[equalizer=frequency={f:g}:width_type=o:width=1:gain={g:g}]")
        out += ["", "# AUDIO EQUALIZER (10 band): OFF"]
        for f, g in zip(EQ_BANDS, self.eq_gains):
            out.append(f"af-remove=lavfi=[equalizer=frequency={f:g}:width_type=o:width=1:gain={g:g}]")
        return "\n".join(out)

    def on_eq_copy(self):
        QGuiApplication.clipboard().setText(self.eq_conf_lines())
        self.status("Equalizer lines copied - paste them into a profile in mpv.conf")

    # -- settings -------------------------------------------------------------------- #
    def setting_value(self, key):
        w = self.sw[key]
        if isinstance(w, QSpinBox):
            return str(w.value())
        if isinstance(w, QComboBox):             # also covers QFontComboBox
            return w.currentText().strip()
        return w.text().strip()

    def collect_settings(self):
        s = {key: self.setting_value(key) for key, _, _ in SETTINGS}
        if IS_WINDOWS:      # backslashes are fragile in quoted mpv.conf values; "/" works
            s["screenshot-directory"] = s["screenshot-directory"].replace("\\", "/")
        return s

    def apply_settings(self, s):
        for key, val in s.items():
            if key == "geometry" or val == "":
                continue
            if key == "sub-font-size":
                try:
                    val = int(val)
                except ValueError:
                    continue
            if key == "screenshot-directory":
                val = os.path.expanduser(val)
                try:
                    os.makedirs(val, exist_ok=True)
                except OSError:
                    pass
            self.cmd("set_property", key, val)

    def browse_screenshot_dir(self):
        start = os.path.expanduser(self.sw["screenshot-directory"].text().strip()) or self.last_dir
        d = QFileDialog.getExistingDirectory(self, "Screenshot folder", start)
        if d:
            self.sw["screenshot-directory"].setText(d)

    def on_settings_apply(self):
        self.apply_settings(self.collect_settings())
        self.status("Settings applied to the running mpv")

    def on_settings_save(self):
        s = self.collect_settings()
        try:
            write_global_conf(s)
        except OSError as e:
            QMessageBox.critical(self, "mpv.conf", f"Could not write {MPV_CONF}:\n{e}")
            return
        self.apply_settings(s)
        self.status(f"Saved to {MPV_CONF} (backup: mpv.conf.bak)")

    # -- playlist: adding ---------------------------------------------------------------- #
    def add_paths(self, paths):
        for p in paths:
            if p:
                self.cmd("loadfile", p, "append-play")

    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add files", self.last_dir, MEDIA_FILTER)
        if files:
            self.last_dir = os.path.dirname(files[0])
            self.add_paths([os.path.normpath(f) for f in files])
            self.status(f"Added {len(files)} files")

    def add_folder_dialog(self, recursive):
        d = QFileDialog.getExistingDirectory(self, "Add folder", self.last_dir)
        if d:
            d = os.path.normpath(d)
            self.last_dir = d
            files = scan_folder(d, recursive)
            self.add_paths(files)
            self.status(f"Added {len(files)} files")

    def add_url_dialog(self):
        clip = QGuiApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(self, "Add URL", "URL (anything yt-dlp or mpv can open):",
                                       text=clip if is_url(clip) else "")
        if ok and url.strip():
            self.add_paths([url.strip()])

    def paste_clipboard(self):
        paths = mime_paths(QGuiApplication.clipboard().mimeData())
        if paths:
            self.handle_drop(paths, -1)
        else:
            self.status("The clipboard has no usable file paths or URLs")

    def handle_drop(self, paths, row=-1):
        """Add dropped/pasted paths; with row >= 0 they are placed before that row."""
        media, subs = expand_inputs(paths)
        if media and self.ensure_mpv():
            before = len(self.last_playlist)
            self.add_paths(media)
            if 0 <= row < before:
                cmds, _ = plan_moves(before + len(media), range(before, before + len(media)), row)
                for i, j in cmds:
                    self.cmd("playlist-move", i, j)
        for s in subs:
            self.cmd("sub-add", s, "select")
        if media or subs:
            parts = ([f"{len(media)} items"] if media else []) + ([f"{len(subs)} subtitles"] if subs else [])
            self.status("Added " + " and ".join(parts))
        else:
            self.status("Nothing playable in what was dropped")

    def load_m3u(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load playlist", self.last_dir,
                                              "Playlists (*.m3u *.m3u8);;All files (*)")
        if path:
            self.last_dir = os.path.dirname(path)
            entries = read_m3u(path)
            self.add_paths(entries)
            self.status(f"Loaded {len(entries)} entries")

    def save_m3u(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save playlist",
                                              os.path.join(self.last_dir, "playlist.m3u"),
                                              "M3U playlist (*.m3u);;UTF-8 M3U playlist (*.m3u8)")
        if path:
            if not Path(path).suffix:
                path += ".m3u"
            lines = ["#EXTM3U"] + [e.get("filename", "") for e in (self.mpv.state.get("playlist") or [])]
            try:
                Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as e:
                QMessageBox.critical(self, "Save playlist", str(e))
                return
            self.last_dir = os.path.dirname(path)
            self.status(f"Saved {path}")

    # -- playlist: editing ----------------------------------------------------------------- #
    def selected_indexes(self):
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def entry(self, index):
        pl = self.mpv.state.get("playlist") or []
        return pl[index] if 0 <= index < len(pl) else {}

    def play_selected(self):
        sel = self.selected_indexes()
        if sel:
            self.cmd("playlist-play-index", sel[0])

    def remove_selected(self):
        for i in reversed(self.selected_indexes()):
            self.cmd("playlist-remove", i)

    def clear_playlist(self):
        self.cmd("playlist-clear")
        self.cmd("stop")

    def move_rows(self, rows, target):
        """Move `rows` as a block to just before row `target` (list.count() = the end)."""
        cmds, order = plan_moves(self.list.count(), rows, target)
        for i, j in cmds:
            self.cmd("playlist-move", i, j)
        if cmds:
            self.pending_select = [order.index(r) for r in sorted(set(rows))]

    def shift_rows(self, direction):
        sel = self.selected_indexes()
        if not sel:
            self.status("Select entries to move first")
            return
        cmds, new_sel = shift_plan(self.list.count(), sel, direction)
        for i, j in cmds:
            self.cmd("playlist-move", i, j)
        if cmds:
            self.pending_select = new_sel

    def reveal(self, path):
        if IS_WINDOWS:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def build_playlist_menu(self):
        sel = self.selected_indexes()
        n = self.list.count()
        menu = QMenu(self)

        def add(target_menu, text, slot, enabled=True):
            a = target_menu.addAction(text)
            a.setEnabled(enabled)
            a.triggered.connect(lambda *_: slot())

        if sel:
            add(menu, "Play", self.play_selected)
            add(menu, "Remove from playlist", self.remove_selected)
            move = menu.addMenu("Move")
            add(move, "Up", lambda: self.shift_rows(-1), sel[0] > 0)
            add(move, "Down", lambda: self.shift_rows(1), sel[-1] < n - 1)
            add(move, "To top", lambda: self.move_rows(sel, 0))
            add(move, "To bottom", lambda: self.move_rows(sel, n))
            names = [self.entry(i).get("filename", "") for i in sel]
            if len(sel) == 1 and names[0] and not is_url(names[0]) and os.path.exists(names[0]):
                add(menu, "Show in folder", lambda: self.reveal(names[0]))
            add(menu, "Copy path / URL", lambda: QGuiApplication.clipboard().setText("\n".join(names)))
            menu.addSeparator()
        add(menu, "Add files…", self.add_files_dialog)
        add(menu, "Add folder…", lambda: self.add_folder_dialog(False))
        add(menu, "Add folder with subfolders…", lambda: self.add_folder_dialog(True))
        add(menu, "Add URL…", self.add_url_dialog)
        clip = QGuiApplication.clipboard().mimeData()
        add(menu, "Paste paths / URLs", self.paste_clipboard,
            clip is not None and (clip.hasUrls() or clip.hasText()))
        menu.addSeparator()
        add(menu, "Select all", self.list.selectAll, n > 0)
        add(menu, "Shuffle", lambda: self.cmd("playlist-shuffle"), n > 1)
        add(menu, "Clear playlist", self.clear_playlist, n > 0)
        return menu

    def show_playlist_menu(self, pos):
        self.build_playlist_menu().exec(self.list.viewport().mapToGlobal(pos))

    # -- drag & drop onto the window itself ------------------------------------------------- #
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            accept_as_copy(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        accept_as_copy(event)

    def dropEvent(self, event):
        self.handle_drop(mime_paths(event.mimeData()), -1)
        accept_as_copy(event)

    # -- periodic UI update ------------------------------------------------------------------ #
    def set_play_button(self, playing):
        self.play_btn.setText("Pause" if playing else "Play")
        self.play_btn.setIcon(self.icon("SP_MediaPause" if playing else "SP_MediaPlay"))

    def refresh_playlist(self):
        if self.list.dragging:                   # never rebuild the list under a running drag
            return
        pl = self.mpv.state.get("playlist") or []
        sig = tuple((e.get("filename"), bool(e.get("current")), e.get("title")) for e in pl)
        if sig == self.playlist_sig:
            return
        self.playlist_sig = sig
        if pl:
            self.last_playlist = [e.get("filename") for e in pl]
        elif self.mpv.alive():
            self.last_playlist = []

        keep = set(self.pending_select if self.pending_select is not None else self.selected_indexes())
        self.pending_select = None
        scroll = self.list.verticalScrollBar().value()
        current_item, current_index = None, None

        self.list.setUpdatesEnabled(False)
        self.list.blockSignals(True)
        self.list.clear()
        for i, e in enumerate(pl):
            playing = bool(e.get("current"))
            item = QListWidgetItem(("▶ " if playing else "   ") + f"{i + 1:>3}. {display_name(e)}")
            item.setToolTip(e.get("filename", ""))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            if playing:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                current_item, current_index = item, i
            self.list.addItem(item)
            if i in keep:
                item.setSelected(True)
        if keep and min(keep) < len(pl):         # keep keyboard focus where the selection is
            self.list.setCurrentRow(min(keep), QItemSelectionModel.SelectionFlag.NoUpdate)
        self.list.blockSignals(False)
        self.list.setUpdatesEnabled(True)
        self.list.verticalScrollBar().setValue(scroll)
        if current_item is not None and current_index != self.current_index:
            self.list.scrollToItem(current_item)  # follow the playing track
        self.current_index = current_index

    def refresh_ui(self):
        if not self.mpv.alive():
            if not self.dead_notified and self.mpv.proc is not None:
                self.dead_notified = True
                self.status("mpv was closed - it restarts when you use a control")
                self.now_label.setText("Idle")
                self.set_play_button(False)
            return
        st = self.mpv.state
        idle = st.get("idle-active", True)
        self.now_label.setText("Idle" if idle else (st.get("media-title") or ""))
        dur, pos = st.get("duration"), st.get("time-pos")
        if not self.seek.isSliderDown():
            self.time_label.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")
            if dur and pos is not None:
                self.set_seek(min(1000, int(pos / dur * 1000)))
            elif idle:
                self.set_seek(0)
        self.set_play_button(not (idle or st.get("pause")))
        muted = bool(st.get("mute"))
        self.mute_btn.setText("Unmute" if muted else "Mute")
        self.mute_btn.setIcon(self.icon("SP_MediaVolumeMuted" if muted else "SP_MediaVolume"))
        vol = st.get("volume")
        if vol is not None and time.time() - self.vol_touch > 1.0 and abs(vol - self.volume) > 0.5:
            self.volume = float(vol)
            self.vol_slider.blockSignals(True)
            self.vol_slider.setValue(int(round(self.volume)))
            self.vol_slider.blockSignals(False)
        self.refresh_playlist()

    def set_seek(self, value):
        self.seek.blockSignals(True)
        self.seek.setValue(value)
        self.seek.blockSignals(False)

    # -- shutdown ------------------------------------------------------------------------------- #
    def closeEvent(self, event):
        self.timer.stop()
        save_app_state({"eq_gains": self.eq_gains, "eq_enabled": self.eq_enabled,
                        "volume": self.volume, "last_dir": self.last_dir,
                        "window": self.saveGeometry().toBase64().data().decode("ascii")})
        self.mpv.quit()
        event.accept()


def main():
    if IS_WINDOWS:
        try:    # own taskbar identity, so the window icon is used instead of python.exe's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("mpv.playlist.qt")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("MPV Playlist")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    win = MainWindow()
    win.show()

    def on_error(exc_type, exc, tb):       # a failing slot must not kill the app silently
        sys.__excepthook__(exc_type, exc, tb)
        win.statusBar().showMessage(f"Error: {exc}", 10000)

    sys.excepthook = on_error
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
