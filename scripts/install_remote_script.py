"""Copy the ClaudeMCP Remote Script into Ableton's User Library.

Live 11+ loads user control surface scripts from
    <User Library>/Remote Scripts/<Name>/
The default User Library is Documents/Ableton/User Library on Windows and
~/Music/Ableton/User Library on macOS; pass a path to use another one:
    python scripts/install_remote_script.py "D:/Ableton/User Library"
Restart Live after the first install, then pick "ClaudeMCP" under
Preferences > Link, Tempo & MIDI > Control Surface.
"""
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "remote_script" / "ClaudeMCP"


def documents_dir() -> Path:
    if sys.platform == "win32":
        import ctypes.wintypes

        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL
        return Path(buf.value)
    return Path.home() / "Documents"


def default_user_library() -> Path:
    candidates = [documents_dir() / "Ableton" / "User Library"]
    if sys.platform == "darwin":
        candidates.insert(0, Path.home() / "Music" / "Ableton" / "User Library")
    return next((c for c in candidates if c.exists()), candidates[0])


def main() -> None:
    user_library = Path(sys.argv[1]) if len(sys.argv) > 1 else default_user_library()
    if not user_library.exists():
        sys.exit(f"User Library not found at {user_library} - pass its path as an argument.")
    dest = user_library / "Remote Scripts" / "ClaudeMCP"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Installed Remote Script to {dest}")


if __name__ == "__main__":
    main()
