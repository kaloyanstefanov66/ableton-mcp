"""Pure-Python music helpers: note naming, quantizing, riff analysis, time mapping, .mid export.

Times are in beats (quarter notes) unless a name says otherwise. Note names follow
Ableton's convention, where MIDI 60 is C3 and 36 (kick in a Drum Rack) is C1.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from pathlib import Path

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Kessler key profiles.
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def note_name(pitch: int) -> str:
    return f"{NAMES[pitch % 12]}{pitch // 12 - 2}"


def quantize(notes: list[dict], grid: float) -> list[dict]:
    """Snap starts and ends to `grid` beats; merge notes that collapse onto each other."""
    if grid <= 0:
        return [dict(n) for n in notes]
    merged: dict[tuple[int, float], dict] = {}
    for n in notes:
        start = round(n["start"] / grid) * grid
        end = round((n["start"] + n["duration"]) / grid) * grid
        if end <= start:
            end = start + grid
        key = (n["pitch"], round(start, 6))
        q = {**n, "start": round(start, 6), "duration": round(end - start, 6)}
        if key not in merged or q["velocity"] > merged[key]["velocity"]:
            merged[key] = q
    return sorted(merged.values(), key=lambda n: (n["start"], n["pitch"]))


def _correlate(a: list[float], b: list[float]) -> float:
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else 0.0


def estimate_key(notes: list[dict]) -> list[dict]:
    """Top key candidates from duration- and velocity-weighted pitch classes."""
    weights = [0.0] * 12
    for n in notes:
        weights[n["pitch"] % 12] += n["duration"] * (n.get("velocity", 100) / 127)
    if not any(weights):
        return []
    scores = []
    for tonic in range(12):
        rotated = weights[tonic:] + weights[:tonic]
        scores.append((_correlate(rotated, MAJOR_PROFILE), f"{NAMES[tonic]} major"))
        scores.append((_correlate(rotated, MINOR_PROFILE), f"{NAMES[tonic]} minor"))
    scores.sort(reverse=True)
    return [{"key": k, "confidence": round(s, 3)} for s, k in scores[:3]]


def analyze(notes: list[dict], beats_per_bar: float = 4.0, grid: float = 0.25) -> dict:
    """Summarize a riff so accompaniment can follow its harmony and rhythm."""
    if not notes:
        return {"note_count": 0}
    pc_weight: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    lowest: dict[int, int] = {}
    onsets: dict[int, set[float]] = defaultdict(set)
    steps = max(1, round(beats_per_bar / grid))
    rhythm = [0] * steps
    for n in notes:
        bar = int(n["start"] // beats_per_bar)
        pos = n["start"] - bar * beats_per_bar
        pc_weight[bar][n["pitch"] % 12] += n["duration"]
        lowest[bar] = min(lowest.get(bar, 128), n["pitch"])
        onsets[bar].add(round(pos, 3))
        step = round(pos / grid)
        if step < steps:
            rhythm[step] += 1
    bars = []
    for bar in sorted(pc_weight):
        pcs = sorted(pc_weight[bar].items(), key=lambda kv: -kv[1])
        bars.append({
            "bar": bar + 1,
            "lowest_note": note_name(lowest[bar]),
            "pitch_classes": [NAMES[pc] for pc, _ in pcs],
            "onsets_in_bar": sorted(onsets[bar]),
        })
    end = max(n["start"] + n["duration"] for n in notes)
    return {
        "note_count": len(notes),
        "length_beats": round(end, 3),
        "range": [note_name(min(n["pitch"] for n in notes)), note_name(max(n["pitch"] for n in notes))],
        "key_candidates": estimate_key(notes),
        "rhythm_profile": {
            "grid_beats": grid,
            "onsets_per_step": rhythm,
            "note": "count of note onsets at each grid step within the bar, summed over all bars",
        },
        "bars": bars,
    }


class PiecewiseLinear:
    """Monotonic piecewise-linear map (e.g. clip beats -> audio-file seconds) with an inverse."""

    def __init__(self, xs: list[float], ys: list[float]) -> None:
        if len(xs) < 2 or len(xs) != len(ys):
            raise ValueError("need at least two matching points")
        self.xs, self.ys = list(xs), list(ys)

    @staticmethod
    def _interp(v: float, src: list[float], dst: list[float]) -> float:
        i = bisect.bisect_right(src, v) - 1
        i = max(0, min(i, len(src) - 2))
        x0, x1, y0, y1 = src[i], src[i + 1], dst[i], dst[i + 1]
        if x1 == x0:
            return y0
        return y0 + (v - x0) * (y1 - y0) / (x1 - x0)

    def forward(self, x: float) -> float:
        return self._interp(x, self.xs, self.ys)

    def inverse(self, y: float) -> float:
        return self._interp(y, self.ys, self.xs)


def write_midi_file(notes: list[dict], path: Path, bpm: float, channel: int = 0,
                    beats_per_bar: int = 4) -> Path:
    import mido

    tpb = 480
    mid = mido.MidiFile(ticks_per_beat=tpb)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=beats_per_bar, denominator=4, time=0))
    events = []
    for n in notes:
        on = round(n["start"] * tpb)
        off = round((n["start"] + n["duration"]) * tpb)
        vel = max(1, min(127, int(round(n.get("velocity", 100)))))
        events.append((on, 1, mido.Message("note_on", note=n["pitch"], velocity=vel, channel=channel)))
        events.append((off, 0, mido.Message("note_off", note=n["pitch"], velocity=0, channel=channel)))
    events.sort(key=lambda e: (e[0], e[1]))  # note_offs before note_ons at the same tick
    now = 0
    for tick, _, msg in events:
        track.append(msg.copy(time=tick - now))
        now = tick
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(path)
    return path
