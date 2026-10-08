import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The Remote Script package (ClaudeMCP) lives outside src/; its introspect module is pure.
sys.path.insert(0, str(ROOT / "remote_script"))
