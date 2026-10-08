"""String IDs shared with the Remote Script, and bar/beat helpers.

IDs are positional (see remote_script/ClaudeMCP/introspect.py): `t3` track, `r0` return,
`m` master, `t3/s2` session clip, `t3/a0` arrangement clip, `t3/d1` device, `t3/mx` mixer,
`<device>/p5` parameter. They stay valid until tracks/clips/devices are added or removed.
"""
from __future__ import annotations

import re

CLIP_RE = re.compile(r"^t(\d+)/([sa])(\d+)$")


def parse_clip(clip_id: str) -> dict:
    """Clip ID -> the (track, slot | arrangement_index) args of the legacy commands."""
    m = CLIP_RE.match(clip_id.strip())
    if not m:
        raise ValueError(f"bad clip id {clip_id!r}: use t<track>/s<slot> or t<track>/a<index>")
    track, kind, index = int(m[1]), m[2], int(m[3])
    if kind == "s":
        return {"track": track, "slot": index}
    return {"track": track, "arrangement_index": index}


def clip_id(track: int, slot: int | None = None, arrangement_index: int | None = None) -> str:
    if slot is not None:
        return f"t{track}/s{slot}"
    if arrangement_index is not None:
        return f"t{track}/a{arrangement_index}"
    raise ValueError("pass slot or arrangement_index")


def beats_per_bar(signature) -> float:
    num, den = signature
    return num * 4 / den


def bar_of(beat: float, bpb: float) -> int:
    """1-based bar containing `beat` (small epsilon so 7.9999 counts as bar 3 in 4/4)."""
    return int((beat + 1e-6) // bpb) + 1


def end_bar(end_beat: float, bpb: float) -> int:
    """Last bar touched by something that ends (exclusively) at `end_beat`."""
    return bar_of(end_beat - 1e-3, bpb)


def bar_start(bar: int, bpb: float) -> float:
    return (bar - 1) * bpb


def pos(beat: float, bpb: float) -> str:
    """Ableton-style position 'bar.beat.sixteenth', all 1-based (beats are quarter notes)."""
    bar = bar_of(beat, bpb)
    within = max(0.0, beat - bar_start(bar, bpb))
    b = int(within + 1e-6)
    sixteenth = int((within - b) * 4 + 1e-6)
    return f"{bar}.{b + 1}.{sixteenth + 1}"
