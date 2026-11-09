# MPV Playlist — Code Walkthrough Tutorial

A guided tour of `mpv_playlist.py`, explaining how each piece works and why it is written that way. Read it top to bottom once, then use it as a reference.

---

## Table of contents

1. [Big picture](#1-big-picture)
2. [Paths, constants and finding mpv](#2-paths-constants-and-finding-mpv)
3. [The mpv JSON IPC client](#3-the-mpv-json-ipc-client)
4. [The Windows named pipe (`WinPipe`)](#4-the-windows-named-pipe-winpipe)
5. [Reading and writing `mpv.conf`](#5-reading-and-writing-mpvconf)
6. [Pure helpers: time, playlists, folders, drag planning](#6-pure-helpers-time-playlists-folders-drag-planning)
7. [Custom widgets](#7-custom-widgets)
8. [Background scanning with `ScanJob`](#8-background-scanning-with-scanjob)
9. [The main window](#9-the-main-window)
10. [Playlist editing: reorder, shift, insert](#10-playlist-editing-reorder-shift-insert)
11. [Settings and equalizer](#11-settings-and-equalizer)
12. [The refresh loop and shutdown](#12-the-refresh-loop-and-shutdown)
13. [Threading model summary](#13-threading-model-summary)
14. [Extension exercises](#14-extension-exercises)

---

## 1. Big picture

The script is a thin GUI over a locally running `mpv`. It never parses media itself; it starts mpv in idle mode, connects to mpv's **JSON IPC** endpoint (a Unix socket on Linux, a named pipe on Windows), and then:

* sends **commands** such as `loadfile`, `playlist-move`, `set_property`;
* receives **property-change events** for `time-pos`, `playlist`, `pause`, etc.

Three layers are easy to spot in the file:

| Layer | Symbols | Thread it runs on |
|---|---|---|
| IPC transport | `MpvIPC`, `WinPipe` | reader/writer threads |
| Pure logic | `scan_folder`, `plan_moves`, `shift_plan`, `read_global_conf`, … | whatever calls them |
| GUI | `MainWindow`, `PlaylistWidget`, `EqSlider`, `SeekSlider` | Qt main thread |

The golden rule throughout: **the GUI thread never blocks on mpv or on the disk**. Commands are queued, folder scans run in threads, and the UI is refreshed by a `QTimer` polling a cached `state` dict.

---

## 2. Paths, constants and finding mpv

```python
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
```

`APP_DIR` works both when running as a script and when frozen with PyInstaller — `sys.executable` is used in the frozen case because `__file__` points inside the bundle.

`find_mpv()` tries, in order: mpv next to the script, `PATH`, then common Windows install locations (`scoop`, `Program Files`). Returning `None` is handled later by `MpvIPC.start`, which raises a `RuntimeError` whose text is `INSTALL_HINT`.

`mpv_config_dir()` respects mpv's *portable mode*: if a `portable_config` folder sits next to `mpv.exe`, that wins. Otherwise the platform default (`%APPDATA%\mpv`, or `~/.config/mpv`) is used.

Extension sets (`MEDIA_EXT`, `PLAYLIST_EXT`, `SUB_EXT`) drive everything that classifies a path. Note `MEDIA_FILTER`, which builds the file-dialog filter string from `MEDIA_EXT` so the two never drift apart.

`EQ_BANDS` / `EQ_LABELS` define the 10-band equalizer; `EQ_PRESETS` are named curves, including `"My mpv.conf curve"` which matches the author's own config.

`SETTINGS` is a list of `(mpv option, label, quote character)` tuples. The third element is important: when writing `mpv.conf`, `ytdl-format` is wrapped in `'…'` and `screenshot-directory` in `"…"`, because those values contain characters that need quoting. `SETTING_DEFAULTS` provides fallbacks when an option is missing from the user's config, and `HWDEC_CHOICES` is platform-aware (`vaapi` on Linux, `d3d11va` etc. on Windows).

---

## 3. The mpv JSON IPC client

### Endpoint naming

```python
if IS_WINDOWS:
    self.ipc_path = rf"\\.\pipe\mpv-playlist-{os.getpid()}"
else:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    self.ipc_path = os.path.join(runtime, f"mpv-playlist-{os.getpid()}.sock")
```

The PID in the name means several copies of the app can run side by side without colliding. On Linux, `XDG_RUNTIME_DIR` is preferred (it is user-private, mode 0700) with a fallback to the temp dir.

### `start()`

```python
self.proc = subprocess.Popen(
    [MPV_EXE, "--idle=yes", "--force-window=yes", "--no-terminal",
     f"--input-ipc-server={self.ipc_path}"], ...)
```

* `--idle=yes` keeps mpv alive with no file loaded (so the GUI can connect immediately).
* `--force-window=yes` makes the window appear right away, even before playback.
* `--no-terminal` avoids a console window and suppresses terminal chatter.
* The user's `mpv.conf` is **not** bypassed; mpv loads it as usual.

Then it retries connecting for up to 8 seconds. This is necessary because mpv needs a moment to create the socket/pipe. Two daemon threads are started (`_reader`, `_writer`) and all observed properties are registered:

```python
for i, name in enumerate(self.OBSERVED, 1):
    self.command("observe_property", 100 + i, name)
```

The ids `101..109` are derived from the index; `PLAYLIST_ID` is computed the same way so that `add_media` can un-observe and re-observe the playlist (see §10).

### Sending commands: the writer thread

```python
def command(self, *args, wait=False, timeout=2.0):
    ...
    self._outbox.put((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
```

`command()` is **non-blocking by default**. It serializes the command, pushes it onto a `queue.Queue`, and returns. The writer thread drains the queue in batches:

```python
chunks = [outbox.get()]
while len(chunks) < 256:
    try:
        chunks.append(outbox.get_nowait())
    except queue.Empty:
        break
data = b"".join(c for c in chunks if c is not None)
```

Batching means that adding 5000 files is one write syscall per batch, not 5000. Sending `None` is the sentinel that stops the thread.

Optional `wait=True` support exists for callers that need the reply (a `threading.Event` per request id). The GUI does not use it in hot paths.

### Receiving: the reader thread

```python
while b"\n" in buf:
    line, buf = buf.split(b"\n", 1)
    ...
    self._dispatch(msg)
```

mpv writes newline-delimited JSON. The reader keeps a buffer because a single `recv` may cut a line in half (or return several lines at once). `_dispatch` splits the two message kinds:

* replies with a `request_id` → wake the waiting `Event` if any;
* `property-change` events → `self.state[name] = msg["data"]`.

`self.state` is therefore a **plain dict read directly by the GUI thread** in `refresh_ui`. Python dict reads/writes of individual keys are atomic under the GIL, which is why no lock is needed around `state`.

### Lifecycle details

* `alive()` checks the flag *and* `proc.poll()` so a crashed mpv is detected even if the threads have not noticed yet.
* `_cleanup_socket()` removes a stale socket file on Linux before starting. On Windows nothing is needed — named pipes disappear with the process.
* `quit()` sends `quit`, waits up to 2 seconds, then kills the process. It also pushes `None` to stop the writer and closes the connection. `WinPipe.close()` cancels pending I/O so a blocked `recv()` unblocks.

---

## 4. The Windows named pipe (`WinPipe`)

This is the subtlest code in the file. On Windows, an mpv IPC pipe can be opened *synchronously* or with `FILE_FLAG_OVERLAPPED`. The synchronous variant deadlocks: if the pipe's write buffer fills while mpv is not reading (e.g. mpv is busy adding thousands of files), our write blocks — and with a *synchronous* handle, the same thread's pending read also blocks, so nothing ever progresses.

`WinPipe` opens the pipe with `FILE_FLAG_OVERLAPPED` (`0x40000000`) and gives the reader and the writer **separate manual-reset events**:

```python
self._rev = _k32.CreateEventW(None, True, False, None)  # used by the reader thread
self._wev = _k32.CreateEventW(None, True, False, None)  # used by the writer thread
```

Each `recv`/`sendall` call creates an `OVERLAPPED` structure bound to the appropriate event and then calls `GetOverlappedResult(..., wait=True)` to wait for completion:

```python
ok = _k32.ReadFile(self.h, buf, size, None, ctypes.byref(ov))
count = self._finish(ok, ov)   # buffer is only valid after _finish returns
return buf.raw[:count]
```

Two remarks:

* The buffer is only read *after* `_finish`. With overlapped I/O, the call returns immediately with `ERROR_IO_PENDING`; the data arrives later.
* `close()` calls `CancelIoEx` to wake a reader parked in `recv()`, then closes all three handles.

Also note all the `argtypes` / `restype` declarations near the top of the file — without them, 64-bit handles get truncated to 32-bit ints by ctypes' default conversion and everything breaks in confusing ways.

The class deliberately mimics the socket API (`recv`, `sendall`, `close`), so `_reader` and `_writer` don't need any `if IS_WINDOWS` branches.

---

## 5. Reading and writing `mpv.conf`

The Settings tab edits the **global section** of `mpv.conf` and leaves everything else (profiles, comments, `[…]` sections) intact.

### Reading

```python
for line in MPV_CONF.read_text(...).splitlines():
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    if s.startswith("["):
        break          # stop at the first profile
    if "=" in s:
        k, v = s.split("=", 1)
        result[k.strip()] = _strip_quotes(v)
```

`_strip_quotes` removes a matching pair of `'` or `"` at both ends. `utf-8-sig` gracefully eats a BOM if a Windows editor added one.

### Writing

`write_global_conf(updates)` works in four steps:

1. **Backup** the existing file to `mpv.conf.bak`.
2. Walk the lines up to the first `[section]`; for each `key=value` line whose key is in `updates`, replace it (or drop it if the new value is empty).
3. For updates whose key never appeared, **insert** them after the last non-empty line of the global section — not at the very end of the file, which could land inside a profile.
4. Create the parent directory and write the result.

The per-option quote character comes from `SETTINGS`, so `ytdl-format=bestvideo…` becomes `ytdl-format='bestvideo…'`.

---

## 6. Pure helpers: time, playlists, folders, drag planning

These functions contain **no Qt and no mpv** calls; they are easy to unit-test.

### `fmt_time`

```python
return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
```

Hours appear only when non-zero, matching typical player conventions.

### `natural_key`

```python
return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
```

Sorts `track9` before `track10`. Used by `scan_folder` for both files and subfolders.

### `read_m3u`

Reads a playlist skipping comments (`#…`), turning relative entries into absolute paths based on the playlist's own directory. URLs are passed through untouched.

### `scan_folder`

```python
with os.scandir(path) as it:
    entries = sorted(it, key=lambda e: natural_key(e.name))
```

`os.scandir` is the fast way to walk a directory: each `DirEntry` caches the type from the directory scan, so `e.is_dir()` and `e.is_file()` usually do **no extra `stat` call** — exactly what makes Windows folder scans slow otherwise.

Recursion is guarded three ways:

* `depth < MAX_DEPTH` (25);
* name in `SKIP_DIRS` (`$recycle.bin`, `System Volume Information`);
* `_is_reparse(e)` — symlinks and Windows junctions are not followed, otherwise a recursive folder could loop forever.

`stop()` (an `Event.is_set` callable) lets a job be cancelled; `progress()` is called every 200 files so the status bar can show a live count.

The traversal collects *files of the current folder first*, then recurses into subfolders — that ordering feels natural in the playlist.

### `expand_inputs`

Normalizes a mixed bag of inputs:

| input | result |
|---|---|
| URL | media entry |
| folder | `scan_folder(...)` result |
| missing path | silently dropped |
| `.m3u`/`.m3u8` | `read_m3u(...)` result |
| `.srt`/`.ass`/… | subtitle list |
| anything else | media entry |

This one function serves file dialogs, drops and paste.

### `mime_paths`

Extracts usable items from a `QMimeData`: URLs (local files via `toLocalFile`, others via `toString`), or plain text split by line, with `"` stripped. Handles the two ways drags and clipboard arrive.

### `plan_moves` and `shift_plan`

These are the trickiest pure functions. They exist because **the GUI does not move list items itself** — it sends `playlist-move i j` to mpv and lets the next playlist event rebuild the list. So the GUI must know exactly which commands produce the intended order.

`plan_moves(count, sources, target)` — drag some rows and drop them *before row `target`* (`target == count` means the end):

```python
order = list(range(count))
moving = sorted(set(sources))
anchor = next((x for x in order[target:] if x not in moving), None)
```

The `anchor` is the first row at-or-after the target that is *not itself being dragged*. Each dragged row `s` is moved to just before the anchor. mpv's rule is "insert entry `i` before whatever is at index `j` **at that moment**", so we simulate the list locally (`order.pop(i)`, `order.insert(...)`) to compute the correct `j` for each step. The function returns both the commands and the final order, so the caller can restore the selection afterwards.

`shift_plan(count, selected, direction)` handles the Up/Down buttons. Rows already at the edge (or blocked by another selected row) stay put, and the returned `new_selection` makes the selection follow the move.

---

## 7. Custom widgets

### `accept_as_copy`

```python
event.setDropAction(Qt.DropAction.CopyAction)
event.accept()
```

A file manager proposes a *move* drop. If we accept it, the manager may **delete the original files** after "moving" them — but we only read their paths. Forcing `CopyAction` prevents data loss. Every drop handler in the file calls this.

### `SeekSlider`

Qt's default slider pages when you click the groove, which is annoying for a seek bar. This subclass jumps to the clicked position:

```python
def _value_at(self, ev):
    span = max(self.width() - 1, 1)
    x = min(max(int(ev.position().x()), 0), span)
    return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)
```

`mousePressEvent` sets the value and marks the slider as "down"; `mouseMoveEvent` keeps following while down; `mouseReleaseEvent` clears the down flag, which emits `sliderReleased` → the seek command. While the slider is down, `refresh_ui` stops overwriting its value, so the user's drag is not fought by mpv's `time-pos` updates.

### `EqSlider`

A vertical slider over `-120..120` = `-12.0..+12.0` dB in 0.1 dB steps. Double-click sets 0:

```python
def mouseDoubleClickEvent(self, ev):
    self.setValue(0)
    ev.accept()
```

### `PlaylistWidget`

A `QListWidget` with three jobs:

**Drag in/out.** `startDrag` is overridden purely to set `self.dragging = True`; `refresh_playlist` reads that flag and skips rebuilding, because rebuilding the list *under a running drag* crashes Qt's drag machinery. Drops are not performed by the widget; it emits signals instead:

```python
if event.source() is self:
    self.rowsMoved.emit(sorted(self.row(i) for i in self.selectedItems()), row)
else:
    self.pathsDropped.emit(mime_paths(event.mimeData()), row)
```

`_row_at` converts a position into an insertion index, biasing to "below" based on the item's vertical center. `_autoscroll` scrolls when the cursor is near the top/bottom edge, which makes long playlists draggable.

**Custom painting.** `paintEvent` draws:

* an empty-state hint ("Drop files, folders or URLs here") when `count() == 0`;
* a 2 px highlight line at the computed drop row.

Qt's built-in drop indicator is disabled (`setDropIndicatorShown(False)`) because it misbehaves for external URL drops; the hand-painted line works for everything.

**Keys.** `Ctrl+V` → paste, `Delete` → remove, `Enter` → play. These are emitted as signals; the window owns the actual logic.

---

## 8. Background scanning with `ScanJob`

```python
class ScanJob(QObject):
    progress = Signal(int)
    done = Signal(list, list, int)      # media, subtitles, row
```

`QObject` + `Signal` lets the worker thread emit results that are delivered to the GUI thread (Qt queues them automatically across threads). `ScanJob.start()` spawns a daemon thread that calls `expand_inputs`, passing `self.stop.is_set` as the cancellation predicate and `self.progress.emit` as the progress callback.

Why a thread at all? Scanning a large folder on a slow disk or network share can take seconds — and doing it inside `dropEvent` would keep *Windows Explorer's* drop operation hanging too. Doing it off-thread returns from `dropEvent` immediately.

The `finished` flag is set before `done` is emitted, so `on_scan_done` can prune the jobs list:

```python
self.jobs = [j for j in self.jobs if not j.finished]
```

`closeEvent` cancels all jobs (`job.stop.set()`) before exiting.

---

## 9. The main window

### Construction order

```python
self.mpv = MpvIPC()
self.saved = load_app_state()
... restore eq gains, volume, last_dir ...
self.build_ui()
self.build_menu()
... restore geometry ...
self.timer = QTimer(...); self.timer.timeout.connect(self.refresh_ui); self.timer.start()
QTimer.singleShot(0, self.start_mpv_initial)
```

`QTimer.singleShot(0, …)` defers starting mpv until the event loop runs — the window is visible before we potentially show a "mpv not found" message box.

`state.json` (next to the script) persists EQ gains, EQ on/off, volume, last folder and window geometry. It is read once in `__init__` and written once in `closeEvent`.

### The player panel (`build_player`)

Straightforward widgets wired to `cmd(...)`:

| widget | action |
|---|---|
| Prev / Next | `playlist-prev` / `playlist-next` |
| Play | `on_play` (see below) |
| Stop | `stop` |
| Mute | `cycle mute` |
| Volume slider | `set_property volume` |
| Speed combo | `set_property speed` |
| Fullscreen | `cycle fullscreen` |
| Subtitles / Audio | `cycle sub` / `cycle audio` |
| Screenshot | `screenshot` |

The Play button is smarter than a plain "cycle pause":

```python
def on_play(self):
    st = self.mpv.state
    if st.get("idle-active") and (st.get("playlist") or []):
        self.cmd("playlist-play-index", 0)
    else:
        self.cmd("cycle", "pause")
```

If mpv is idle but the playlist is not empty, Play starts the first entry instead of doing nothing.

### `cmd` / `ensure_mpv`

```python
def cmd(self, *args):
    if self.ensure_mpv():
        self.mpv.command(*args)
```

Every user action goes through `cmd`. If mpv died (window closed, crash), `ensure_mpv` restarts it, and `start_mpv`'s `restart` logic re-loads `self.last_playlist` so the playlist survives. `last_playlist` is kept up to date in `refresh_playlist`.

### Dialogs

* `add_files_dialog` — multi-file open; remembers the folder in `last_dir`.
* `add_folder_dialog(recursive)` — folder chooser; delegates to `handle_drop`.
* `add_url_dialog` — pre-fills with the clipboard if it looks like a URL.
* `paste_clipboard` — clipboard → `mime_paths` → `handle_drop`.
* `load_m3u` / `save_m3u` — save writes `#EXTM3U` plus each entry's `filename`.

### Right-click menu (`build_playlist_menu`)

Built on demand. Selection-dependent items (Play, Remove, Move submenu, Show in folder, Copy path) only appear with a selection. "Show in folder" is only offered for a single existing local file. The Paste item is enabled only if the clipboard actually contains URLs or text.

`reveal()` deserves a comment: on Windows it passes a **single string**

```python
subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
```

rather than a list, because a list makes Python quote the whole `/select,path` token when the path has spaces, and Explorer then interprets it as "no selection" and opens Documents.

---

## 10. Playlist editing: reorder, shift, insert

### Mirroring principle

`refresh_playlist` rebuilds the list from `mpv.state["playlist"]` whenever a cheap signature changes:

```python
sig = tuple((e.get("filename"), bool(e.get("current")), e.get("title")) for e in pl)
if sig == self.playlist_sig:
    return
```

This is why all edits are sent to mpv rather than done locally — there is exactly one source of truth. Two details keep this usable:

* **Selection survives** rebuilds: `keep` is the current selection (or `pending_select` if a move is in flight), and rows in `keep` are re-selected after `clear()`/re-add.
* **Scroll position survives**: saved before and restored after the rebuild; and if the *current* track changed, the list scrolls to it.

The per-item flags strip `ItemIsDropEnabled`:

```python
item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
```

so a drop always means "insert between rows", never "drop onto a row".

### Inserting at a position (`add_media`)

New files always go through `loadfile <path> append-play` (append at the end). If they should land *before* an existing row `row`, `plan_moves` computes the `playlist-move` commands to relocate them.

For large batches there is an optimization:

```python
bulk = len(media) > 20
if bulk:
    self.mpv.command("unobserve_property", self.mpv.PLAYLIST_ID)
for p in media:
    self.mpv.command("loadfile", p, "append-play")
...
if bulk:
    self.mpv.command("observe_property", self.mpv.PLAYLIST_ID, "playlist")
```

mpv sends the **entire playlist** after every single `loadfile`. For 5000 files that is 5000 full-playlist events, and the GUI would waste enormous time rebuilding. Un-observing the property for the duration, then re-observing, delivers one final update instead. Note that `before` is captured before appending because mpv state is not updated synchronously.

### Removing and clearing

```python
for i in reversed(self.selected_indexes()):
    self.cmd("playlist-remove", i)
```

Removing **back to front** is essential: removing index 2 shifts everything after it down by one.

### Moving rows (`move_rows`, `shift_rows`)

Both follow the same pattern: ask the pure helper for commands, send them, then set `pending_select` so that when the next playlist event arrives, the moved rows are selected again.

```python
def move_rows(self, rows, target):
    cmds, order = plan_moves(self.list.count(), rows, target)
    for i, j in cmds:
        self.cmd("playlist-move", i, j)
    if cmds:
        self.pending_select = [order.index(r) for r in sorted(set(rows))]
```

---

## 11. Settings and equalizer

### Settings tab

Fields are created per entry of `SETTINGS`, with a type switch:

* `hwdec` → editable `QComboBox` (presets but free text);
* `sub-auto` → fixed combo;
* `sub-font` → `QFontComboBox` (all installed fonts, still editable);
* `sub-font-size` → `QSpinBox` 10–160;
* everything else → `QLineEdit`.

`screenshot-directory` gets a Browse button in an `QHBoxLayout` next to the line edit.

**Apply to running mpv** (`apply_settings`) skips two things:

* `geometry` — mpv only reads it at startup;
* empty values — they mean "not set", not "set to empty".

It expands `~` and creates the screenshot folder. **Save to mpv.conf** writes through `write_global_conf` and then applies the same values live, so both stay in sync.

Windows backslashes are normalized to `/` in `collect_settings` before writing — quoted values with backslashes are fragile in `mpv.conf`, while forward slashes work everywhere.

### Equalizer

The EQ is implemented as a **labelled lavfi filter** named `@eq`:

```python
def apply_eq(self):
    self.mpv.command("af", "remove", "@eq")
    graph = eq_graph(self.eq_gains)
    if self.eq_enabled and graph:
        self.mpv.command("af", "add", f"@eq:lavfi=[{graph}]")
```

Because it has a label, applying a new curve is just "remove `@eq`, add `@eq`" — no need to reason about which other audio filters are in the chain (mpv's `af` is an ordered list).

`eq_graph`:

```python
parts = [f"equalizer=frequency={f:g}:width_type=o:width=1:gain={g:g}"
         for f, g in zip(EQ_BANDS, gains) if abs(g) > 1e-9]
```

Skips 0 dB bands (fewer filter instances) and uses `:g` formatting so `31` prints as `31`, not `31.0`. `width_type=o:width=1` means one octave bandwidth, so the 10 bands tile the spectrum.

Slider changes are **debounced**:

```python
self.eq_timer = QTimer(self); self.eq_timer.setSingleShot(True)
self.eq_timer.setInterval(150)
self.eq_timer.timeout.connect(self.apply_eq)
```

Dragging a slider fires dozens of `valueChanged` signals per second; re-building a 10-band filter graph that often would crackle. The timer restarts on each change and only fires 150 ms after the last one.

`eq_conf_lines()` produces ready-to-paste `mpv.conf` lines — an `af-add` set for the "ON" profile and the matching `af-remove` set for the "OFF" profile.

---

## 12. The refresh loop and shutdown

### `refresh_ui` — every 200 ms

```python
if self.watchdog:
    faulthandler.dump_traceback_later(5, file=self.watchdog)
```

When `MPV_PL_DEBUG=1` is set, each tick *re-arms* a 5-second faulthandler timer. If the GUI thread ever stalls for 5 seconds, every thread's stack is dumped to `debug.log` — the timer would have been cancelled by the next tick otherwise.

Then the function:

1. Detects a dead mpv (`if not self.mpv.alive()`) and shows a one-time status message with `self.mpv.error` (the last transport error, e.g. `read: …`).
2. Updates the title label, time label and play/pause button from `state`.
3. Updates the seek slider — **unless the user is dragging it**.
4. Updates the volume slider — **unless the user touched it in the last second** (`vol_touch`), to avoid the slider jumping while being dragged.
5. Calls `refresh_playlist`.

All of this reads `self.mpv.state` directly; no IPC round-trip per tick.

### Shutdown (`closeEvent`)

```python
self.timer.stop()
faulthandler.cancel_dump_traceback_later()
for job in self.jobs:
    job.stop.set()
save_app_state({...})
self.mpv.quit()
event.accept()
```

Order matters: stop the timer first (so no refresh runs against a dying mpv), cancel the watchdog, cancel scans, persist state, then quit mpv.

### Error hook

```python
def on_error(exc_type, exc, tb):
    sys.__excepthook__(exc_type, exc, tb)
    win.statusBar().showMessage(f"Error: {exc}", 10000)
```

PySide6 swallows exceptions raised inside slots unless a handler is installed; without this a bug in a slot would look like "the button does nothing". The default hook still prints the full traceback to stderr.

---

## 13. Threading model summary

| Thread | Runs | Talks to others via |
|---|---|---|
| GUI (Qt main) | all widgets, `refresh_ui`, `add_media`, `move_rows` | `self.mpv.state` (dict reads), `self.mpv.command` (queue put) |
| mpv reader | `MpvIPC._reader` | writes `self.mpv.state`, sets `_pending` events |
| mpv writer | `MpvIPC._writer` | drains `_outbox`, writes to socket/pipe |
| scan threads | `ScanJob._run` | emits Qt signals (`progress`, `done`) → queued to GUI thread |

Rules of thumb visible in the code:

* **Never write to the mpv socket from the GUI thread** — always go through `command()`.
* **Never block in a Qt event handler** — folder scans and mpv startup are the two slow things, and both are off-loaded (thread / timeout loop).
* **Cross-thread communication is one-directional** where possible: GUI → mpv is a queue; mpv → GUI is a shared dict; scans → GUI are Qt signals.
* **Pure functions are pure** — `plan_moves`, `shift_plan`, `scan_folder`, `eq_graph` take plain data and return plain data; they are the safest places to make changes.

---

## 14. Extension exercises

Good ways to practice with this codebase, roughly in increasing difficulty:

1. **Add a speed option** `2.5` to the combo — trivial; then make the combo *reflect* mpv's actual `speed` (add `"speed"` to `MpvIPC.OBSERVED`, update the combo in `refresh_ui` while blocking its signals).
2. **Add a "Recent folders" submenu** in the playlist context menu, stored in `state.json`.
3. **Add a `loop-combo` synchronization**: after `on_loop`, verify that `refresh_ui` reflects mpv's `loop-playlist`/`loop-file` values when changed from elsewhere (e.g. an `input.conf` key binding).
4. **Improve `plan_moves`** to produce fewer commands for multi-row drags (a minimum number of moves), and add tests for edge cases: empty selection, dropping onto itself, dropping at the end.
5. **Add a "Fade on pause"** filter using a second labeled `af` filter and a `pause` property observer — practice using the labelled-filter pattern from `apply_eq`.
6. **Persist the equalizer *enabled* state per file** (e.g. disable for `.flac`), which requires reading `media-title`/`filename` in `refresh_ui` and toggling `apply_eq` — good practice with the event-driven model.

Each of these touches a different layer, so by the time you have done three or four you will know the whole file.

---

*End of tutorial.*