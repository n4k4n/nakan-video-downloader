"""Build the app with PyInstaller for the current OS.

    pip install -r requirements-build.txt
    python build.py

Windows -> dist/Nakan Video Downloader.exe (single file)
macOS   -> dist/Nakan Video Downloader.app
"""

import shutil
import sys
from pathlib import Path

import imageio_ffmpeg
import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""
SEP = ";" if IS_WIN else ":"

# Stage the ffmpeg binary from imageio-ffmpeg under a plain name, so yt-dlp finds it.
stage = ROOT / "build" / "ffmpeg_bin"
stage.mkdir(parents=True, exist_ok=True)
ffmpeg_dst = stage / f"ffmpeg{EXE}"
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_dst)
ffmpeg_dst.chmod(0o755)

args = [
    str(ROOT / "nakan_video_downloader.py"),
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "Nakan Video Downloader",
    "--icon", str(ROOT / "assets" / "icon.png"),
    "--collect-data", "customtkinter",
    "--add-binary", f"{ffmpeg_dst}{SEP}ffmpeg_bin",
]
if IS_WIN:
    args.append("--onefile")
if IS_MAC:
    args += ["--osx-bundle-identifier", "me.nakan.videodownloader"]

PyInstaller.__main__.run(args)
