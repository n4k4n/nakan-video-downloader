"""
Nakan Video Downloader
A small desktop front-end for yt-dlp that works on Windows and macOS
without installing anything by hand.

- ffmpeg ships inside the app (from imageio-ffmpeg, built with libx264).
- yt-dlp and Deno (the JavaScript runtime YouTube now requires) are fetched
  from their official GitHub releases on first launch and kept in the user's
  app-data folder. yt-dlp updates itself once a day.
"""

import json
import os
import platform
import queue
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

import certifi
import customtkinter as ctk
from tkinter import filedialog

APP_NAME = "Nakan Video Downloader"
APP_ID = "NakanVideoDownloader"
VERSION = "1.0.0"
SITE_URL = "https://nakan.me/freebies"

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""
MACHINE = platform.machine().lower()
IS_ARM = MACHINE in ("arm64", "aarch64")


# ---------------------------------------------------------------- paths

def app_data_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA = app_data_dir()
BIN = DATA / "bin"
BIN.mkdir(exist_ok=True)
YTDLP = BIN / f"yt-dlp{EXE}"
DENO = BIN / f"deno{EXE}"
SETTINGS_FILE = DATA / "settings.json"
STATE_FILE = DATA / "state.json"


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def ffmpeg_path() -> Path:
    """ffmpeg bundled by build.py, or imageio-ffmpeg's copy when running from source."""
    bundled = resource_dir() / "ffmpeg_bin" / f"ffmpeg{EXE}"
    if bundled.exists():
        return bundled
    local = BIN / f"ffmpeg{EXE}"
    if not local.exists():
        import imageio_ffmpeg
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), local)
        make_executable(local)
    return local


def default_download_dir() -> str:
    d = Path.home() / "Downloads"
    return str(d if d.exists() else Path.home())


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def make_executable(p: Path) -> None:
    if not IS_WIN:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if IS_MAC:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(p)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- components

def ytdlp_url() -> str:
    base = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
    if IS_WIN:
        return base + "yt-dlp.exe"
    if IS_MAC:
        return base + "yt-dlp_macos"
    return base + ("yt-dlp_linux_aarch64" if IS_ARM else "yt-dlp_linux")


def deno_url() -> str:
    if IS_WIN:
        triple = "x86_64-pc-windows-msvc"
    elif IS_MAC:
        triple = ("aarch64" if IS_ARM else "x86_64") + "-apple-darwin"
    else:
        triple = ("aarch64" if IS_ARM else "x86_64") + "-unknown-linux-gnu"
    return f"https://github.com/denoland/deno/releases/latest/download/deno-{triple}.zip"


SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def http_download(url: str, dest: Path, on_progress) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_ID}/{VERSION}"})
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            on_progress(done, total)
    os.replace(tmp, dest)


def install_ytdlp(on_progress) -> None:
    http_download(ytdlp_url(), YTDLP, on_progress)
    make_executable(YTDLP)


def install_deno(on_progress) -> None:
    zpath = BIN / "deno.zip"
    http_download(deno_url(), zpath, on_progress)
    with zipfile.ZipFile(zpath) as z:
        member = next(n for n in z.namelist() if Path(n).name == f"deno{EXE}")
        with z.open(member) as src, open(DENO, "wb") as dst:
            shutil.copyfileobj(src, dst)
    zpath.unlink(missing_ok=True)
    make_executable(DENO)


def popen_kwargs() -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def kill_tree(proc: subprocess.Popen) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.kill()


# ---------------------------------------------------------------- options

FORMATS = {
    "MP4 for editing (H.264)": "editor",
    "Original (no conversion)": "original",
    "Audio only (MP3)": "audio",
}
RESOLUTIONS = {"Best available": None, "2160p (4K)": 2160, "1440p": 1440, "1080p": 1080, "720p": 720}
COOKIES = {"None": None, "Chrome": "chrome", "Firefox": "firefox", "Edge": "edge",
           "Safari": "safari", "Brave": "brave"}

PROGRESS_TAG = "__P__"
DONE_TAG = "__DONE__"


def build_ytdlp_cmd(url: str, outdir: str, fmt: str, res, cookies, playlist: bool) -> list:
    cmd = [
        str(YTDLP), "--ignore-config", "--newline", "--progress", "--color", "never",
        "--ffmpeg-location", str(ffmpeg_path().parent),
        "--js-runtimes", f"deno:{DENO}",
        "-P", outdir,
        "-o", "%(title).150B [%(id)s].%(ext)s",
        "--progress-template",
        f"download:{PROGRESS_TAG} %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(info.title)s",
        "--print", f"after_move:{DONE_TAG} %(filepath)s",
        "--no-simulate",
        "--yes-playlist" if playlist else "--no-playlist",
    ]
    res_key = f"res:{res}" if res else "res"
    if fmt == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif fmt == "editor":
        cmd += ["-f", "bv*+ba/b", "-S", f"{res_key},fps,vcodec:h264,acodec:m4a",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bv*+ba/b", "-S", f"{res_key},fps", "--merge-output-format", "mp4/mkv"]
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    cmd.append(url)
    return cmd


def probe(path: str) -> dict:
    """Read codecs/duration with ffmpeg -i (no ffprobe needed)."""
    kw = popen_kwargs()
    p = subprocess.run([str(ffmpeg_path()), "-hide_banner", "-i", path],
                       **{k: v for k, v in kw.items() if k not in ("bufsize",)},
                       timeout=60)
    out = p.stdout or ""
    v = re.search(r"Stream #\S+.*?: Video: (\w+)", out)
    a = re.search(r"Stream #\S+.*?: Audio: (\w+)", out)
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    return {"vcodec": v[1] if v else None, "acodec": a[1] if a else None, "duration": dur}


def friendly_hint(log_text: str, cookies) -> str:
    t = log_text.lower()
    if "could not copy chrome cookie database" in t or "failed to decrypt with dpapi" in t:
        return "Couldn't read Chrome's cookies. Close Chrome completely, or pick Firefox in Cookies."
    if any(s in t for s in ("sign in to confirm", "login required", "requested content is not available",
                            "use --cookies", "rate-limit reached", "private video")):
        if not cookies:
            return "This link needs you to be logged in. Pick the browser where you're logged in under Cookies and try again."
        return "The site refused the download even with cookies. Make sure you're logged in on that browser."
    if "unsupported url" in t:
        return "This site isn't supported."
    if "unable to download" in t or "connection" in t:
        return "Network error. Check your connection and try again."
    return ""


# ---------------------------------------------------------------- UI

class App(ctk.CTk):
    ACCENT = "#e0453a"
    ACCENT_HOVER = "#c23a30"

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("680x640")
        self.minsize(600, 580)
        self.q: "queue.Queue" = queue.Queue()
        self.proc = None
        self.cancelled = False
        self.busy = False
        self.ready = False
        self.last_file = None
        self.settings = load_json(SETTINGS_FILE)
        self._build()
        self.after(100, self._poll)
        threading.Thread(target=self._setup_worker, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout
    def _build(self):
        pad = {"padx": 20}
        self.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(18, 6), **pad)
        ctk.CTkLabel(head, text=APP_NAME, font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="YouTube, Reddit, Instagram, Vimeo, X and 1000+ other sites",
                     text_color="gray65").pack(anchor="w")

        url_row = ctk.CTkFrame(self, fg_color="transparent")
        url_row.grid(row=1, column=0, sticky="ew", pady=(8, 0), **pad)
        url_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(url_row, text="Links (one per line)").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(url_row, text="Paste", width=70, height=24, fg_color="gray25",
                      hover_color="gray30", command=self._paste).grid(row=0, column=1, sticky="e")
        self.urls = ctk.CTkTextbox(self, height=90, wrap="none")
        self.urls.grid(row=2, column=0, sticky="ew", pady=(4, 10), **pad)

        opts = ctk.CTkFrame(self)
        opts.grid(row=3, column=0, sticky="ew", **pad)
        for c in range(3):
            opts.grid_columnconfigure(c, weight=1)
        def saved(key, options):
            v = self.settings.get(key)
            return v if v in options else list(options)[0]
        self.fmt = ctk.StringVar(value=saved("format", FORMATS))
        self.res = ctk.StringVar(value=saved("res", RESOLUTIONS))
        self.cookies = ctk.StringVar(value=saved("cookies", COOKIES))
        for col, (label, var, values) in enumerate([
            ("Format", self.fmt, list(FORMATS)),
            ("Max resolution", self.res, list(RESOLUTIONS)),
            ("Cookies from browser", self.cookies, list(COOKIES)),
        ]):
            ctk.CTkLabel(opts, text=label, text_color="gray70").grid(row=0, column=col, sticky="w", padx=10, pady=(8, 0))
            ctk.CTkOptionMenu(opts, variable=var, values=values, dynamic_resizing=False,
                              fg_color="gray25", button_color="gray30", button_hover_color="gray35",
                              command=lambda _=None: self._save_settings()
                              ).grid(row=1, column=col, sticky="ew", padx=10, pady=(2, 6))
        self.playlist = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Download the whole playlist when the link has one",
                        variable=self.playlist).grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 10))

        folder = ctk.CTkFrame(self, fg_color="transparent")
        folder.grid(row=4, column=0, sticky="ew", pady=(10, 0), **pad)
        folder.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(folder, text="Save to").grid(row=0, column=0, padx=(0, 8))
        self.outdir = ctk.StringVar(value=self.settings.get("outdir", default_download_dir()))
        ctk.CTkEntry(folder, textvariable=self.outdir).grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(folder, text="Change…", width=90, fg_color="gray25", hover_color="gray30",
                      command=self._pick_folder).grid(row=0, column=2, padx=(8, 0))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", pady=(14, 0), **pad)
        actions.grid_columnconfigure(0, weight=1)
        self.dl_btn = ctk.CTkButton(actions, text="Preparing…", height=40, state="disabled",
                                    fg_color=self.ACCENT, hover_color=self.ACCENT_HOVER,
                                    font=ctk.CTkFont(size=15, weight="bold"), command=self._start)
        self.dl_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ctk.CTkButton(actions, text="Cancel", width=90, height=40, state="disabled",
                                        fg_color="gray25", hover_color="gray30", command=self._cancel)
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

        self.bar = ctk.CTkProgressBar(self, progress_color=self.ACCENT)
        self.bar.set(0)
        self.bar.grid(row=6, column=0, sticky="ew", pady=(14, 4), **pad)
        self.status = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=620)
        self.status.grid(row=7, column=0, sticky="ew", **pad)

        self.log = ctk.CTkTextbox(self, height=110, font=ctk.CTkFont(family="Courier", size=11),
                                  text_color="gray70", state="disabled")
        self.log.grid(row=8, column=0, sticky="nsew", pady=(6, 6), **pad)
        self.grid_rowconfigure(8, weight=1)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=9, column=0, sticky="ew", pady=(0, 14), **pad)
        foot.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(foot, text="Open folder", width=110, fg_color="gray25", hover_color="gray30",
                      command=self._open_folder).grid(row=0, column=0)
        link = ctk.CTkLabel(foot, text=f"v{VERSION} · nakan.me", text_color="gray55", cursor="hand2")
        link.grid(row=0, column=2, sticky="e")
        link.bind("<Button-1>", lambda e: webbrowser.open(SITE_URL))

    # ---- helpers
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _paste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        cur = self.urls.get("1.0", "end").strip()
        self.urls.insert("end", ("\n" if cur else "") + text)

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or default_download_dir())
        if d:
            self.outdir.set(d)
            self._save_settings()

    def _open_folder(self):
        path = self.outdir.get()
        if not os.path.isdir(path):
            return
        if IS_WIN:
            if self.last_file and os.path.exists(self.last_file):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(self.last_file)])
            else:
                os.startfile(path)
        elif IS_MAC:
            if self.last_file and os.path.exists(self.last_file):
                subprocess.Popen(["open", "-R", self.last_file])
            else:
                subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _save_settings(self):
        self.settings.update(format=self.fmt.get(), res=self.res.get(),
                             cookies=self.cookies.get(), outdir=self.outdir.get())
        save_json(SETTINGS_FILE, self.settings)

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.dl_btn.configure(state="disabled" if busy or not self.ready else "normal",
                              text="Downloading…" if busy else "Download")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    # ---- queue pump (worker threads never touch widgets)
    def _poll(self):
        try:
            while True:
                kind, *args = self.q.get_nowait()
                if kind == "status":
                    self.status.configure(text=args[0], text_color=args[1] if len(args) > 1 else "gray85")
                elif kind == "progress":
                    if args[0] is None:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start()
                    else:
                        self.bar.stop()
                        self.bar.configure(mode="determinate")
                        self.bar.set(max(0.0, min(1.0, args[0])))
                elif kind == "log":
                    self._log(args[0])
                elif kind == "ready":
                    self.ready = True
                    self._set_busy(False)
                elif kind == "done":
                    self._set_busy(False)
                elif kind == "file":
                    self.last_file = args[0]
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def put(self, *msg):
        self.q.put(msg)

    # ---- first launch / updates
    def _setup_worker(self):
        try:
            for name, path, installer in (("yt-dlp", YTDLP, install_ytdlp), ("Deno", DENO, install_deno)):
                if path.exists():
                    continue
                self.put("status", f"First launch: downloading {name} (only once)…")

                def prog(done, total, name=name):
                    if total:
                        self.put("progress", done / total)
                        self.put("status", f"First launch: downloading {name}… {done / 1e6:.0f} / {total / 1e6:.0f} MB")
                installer(prog)
                self.put("log", f"Installed {name} → {path}")

            self.put("progress", None)
            self.put("status", "Preparing ffmpeg…")
            ffmpeg_path()

            state = load_json(STATE_FILE)
            if time.time() - state.get("last_update", 0) > 24 * 3600:
                self.put("status", "Checking for yt-dlp updates…")
                try:
                    p = subprocess.run([str(YTDLP), "-U"], **{k: v for k, v in popen_kwargs().items()
                                                            if k != "bufsize"}, timeout=120)
                    lines = (p.stdout or "").strip().splitlines()
                    if p.returncode == 0:
                        if lines:
                            self.put("log", lines[-1])
                        state["last_update"] = time.time()
                        save_json(STATE_FILE, state)
                    else:
                        self.put("log", "Couldn't check for yt-dlp updates now; will try again next launch.")
                except Exception as e:
                    self.put("log", f"Update check skipped: {e}")

            self.put("progress", 0)
            self.put("status", "Ready. Paste a link and hit Download.")
            self.put("ready")
        except Exception as e:
            self.put("progress", 0)
            self.put("status", f"Couldn't download the components: {e}\nCheck your internet connection and reopen the app.", "#ff7b72")
            self.put("log", repr(e))

    # ---- download
    def _start(self):
        urls = [u.strip() for u in self.urls.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            self.status.configure(text="Paste at least one link.", text_color="#ffb86b")
            return
        outdir = self.outdir.get().strip()
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
        except Exception:
            self.status.configure(text="That folder doesn't exist.", text_color="#ff7b72")
            return
        self._save_settings()
        self.cancelled = False
        self._set_busy(True)
        opts = dict(outdir=outdir, fmt=FORMATS[self.fmt.get()], res=RESOLUTIONS[self.res.get()],
                    cookies=COOKIES[self.cookies.get()], playlist=self.playlist.get())
        threading.Thread(target=self._download_worker, args=(urls, opts), daemon=True).start()

    def _cancel(self):
        self.cancelled = True
        kill_tree(self.proc)
        self.put("status", "Cancelled.", "#ffb86b")

    def _download_worker(self, urls, o):
        ok, failed = 0, 0
        for i, url in enumerate(urls, 1):
            if self.cancelled:
                break
            prefix = f"[{i}/{len(urls)}] " if len(urls) > 1 else ""
            self.put("progress", None)
            self.put("status", f"{prefix}Fetching info…")
            self.put("log", f"→ {url}")
            files, log_lines = [], []
            cmd = build_ytdlp_cmd(url, o["outdir"], o["fmt"], o["res"], o["cookies"], o["playlist"])
            try:
                self.proc = subprocess.Popen(cmd, **popen_kwargs())
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if line.startswith(PROGRESS_TAG):
                        parts = line[len(PROGRESS_TAG):].strip().split("|", 3)
                        pct = parts[0].strip().rstrip("%")
                        try:
                            self.put("progress", float(pct) / 100)
                        except ValueError:
                            pass
                        title = parts[3] if len(parts) > 3 else ""
                        speed = parts[1].strip() if len(parts) > 1 else ""
                        eta = parts[2].strip() if len(parts) > 2 else ""
                        self.put("status", f"{prefix}{title[:70]}\n{parts[0].strip()}  ·  {speed}  ·  ETA {eta}")
                    elif line.startswith(DONE_TAG):
                        files.append(line[len(DONE_TAG):].strip())
                    elif line:
                        log_lines.append(line)
                        self.put("log", line)
                self.proc.wait()
            except Exception as e:
                log_lines.append(str(e))
                self.put("log", str(e))
            if self.cancelled:
                break
            if self.proc and self.proc.returncode == 0 and files:
                for f in files:
                    if o["fmt"] == "editor":
                        f = self._make_editor_friendly(f, prefix) or f
                    self.put("file", f)
                    self.put("log", f"✓ {f}")
                ok += 1
            else:
                failed += 1
                hint = friendly_hint("\n".join(log_lines), o["cookies"])
                self.put("status", f"{prefix}Download failed. {hint}".strip(), "#ff7b72")
                self.put("log", f"✗ {url}")
        self.proc = None
        self.put("progress", 0 if failed or self.cancelled else 1)
        if not self.cancelled:
            if failed and not ok:
                pass  # keep the error message on screen
            elif failed:
                self.put("status", f"{ok} downloaded, {failed} failed — see details below.", "#ffb86b")
            else:
                self.put("status", "Done! " + ("Click 'Open folder' to see it." if ok == 1 else f"{ok} downloads finished."), "#7ee787")
        self.put("done")

    def _make_editor_friendly(self, path: str, prefix: str):
        """Re-encode to H.264/AAC MP4 only when the downloaded file isn't already."""
        if not os.path.exists(path):
            return None
        info = probe(path)
        v_ok = info["vcodec"] in (None, "h264")
        a_ok = info["acodec"] in (None, "aac")
        if v_ok and a_ok and path.lower().endswith(".mp4"):
            return path
        what = "video" if not v_ok else "audio"
        self.put("status", f"{prefix}Converting {what} to H.264 / AAC for editing apps…")
        self.put("progress", 0)
        base, _ = os.path.splitext(path)
        out = base + ".mp4"
        tmp = base + ".converting.mp4"
        cmd = [str(ffmpeg_path()), "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
               "-i", path, "-map", "0:v:0?", "-map", "0:a:0?"]
        cmd += ["-c:v", "copy"] if v_ok else ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "copy"] if a_ok else ["-c:a", "aac", "-b:a", "320k"]
        cmd += ["-movflags", "+faststart", tmp]
        self.proc = subprocess.Popen(cmd, **popen_kwargs())
        dur = info["duration"] or 0
        for line in self.proc.stdout:
            if line.startswith("out_time_us=") and dur:
                try:
                    self.put("progress", int(line.split("=")[1]) / 1e6 / dur)
                except ValueError:
                    pass
        self.proc.wait()
        if self.proc.returncode != 0 or self.cancelled:
            if os.path.exists(tmp):
                os.remove(tmp)
            if not self.cancelled:
                self.put("log", "Conversion failed; kept the original file.")
            return path
        os.replace(tmp, out)
        if os.path.abspath(out) != os.path.abspath(path):
            os.remove(path)
        return out

    def _on_close(self):
        self.cancelled = True
        kill_tree(self.proc)
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
