"""Install the ableton-drums Claude Skill into ~/.claude/skills/ (user scope).

Restart Claude Code (or start a new session) afterwards so it picks the skill up.
"""
import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "skills" / "ableton-drums"


def main() -> None:
    dest = Path.home() / ".claude" / "skills" / "ableton-drums"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest)
    print(f"Installed skill to {dest}")


if __name__ == "__main__":
    main()
