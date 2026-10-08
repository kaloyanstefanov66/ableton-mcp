"""Understand existing MIDI: grid/feel, density, dynamics, form, phrases, drum patterns.

Input notes are dicts {pitch, start, duration, velocity} in beats. For arrangement clips the
server first expands them to song time (expand_clip_notes), so bars are song bars.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean, median, pstdev

from . import music
from .refs import bar_of, bar_start

GRIDS = [("1/4", 1.0), ("1/8", 0.5), ("1/8T", 1 / 3), ("1/16", 0.25), ("1/16T", 1 / 6), ("1/32", 0.125)]
STEP = 0.25  # pattern resolution (16ths)

GM_DRUMS = {35: "kick", 36: "kick", 37: "sidestick", 38: "snare", 39: "clap", 40: "snare",
            41: "tom", 42: "hihat", 43: "tom", 44: "hihat_pedal", 45: "tom", 46: "hihat_open",
            47: "tom", 48: "tom", 49: "crash", 50: "tom", 51: "ride", 52: "china", 53: "ride_bell",
            55: "crash", 57: "crash", 59: "ride"}
TIMEKEEPERS = ("hihat", "hihat_open", "ride", "ride_bell", "crash", "china")


# ------------------------------------------------------------------ helpers

def expand_clip_notes(clip: dict) -> list[dict]:
    """Remote `clip_notes`/`arrangement_notes` clip -> note dicts, in song time for
    arrangement clips (follows looping and the start marker); muted notes dropped."""
    notes = [{"pitch": n[0], "start": n[1], "duration": n[2], "velocity": n[3]}
             for n in clip["notes"] if not n[4]]
    if clip.get("start_time") is None:
        return notes
    start, end = clip["start_time"], clip["end_time"]
    marker = clip["start_marker"]
    ls, le = clip["loop_start"], clip["loop_end"]
    out = []
    if not clip.get("looping") or le - ls <= 1e-6:
        span = end - start
        for n in notes:
            if marker - 1e-6 <= n["start"] < marker + span - 1e-6:
                out.append({**n, "start": round(start + n["start"] - marker, 4)})
        return out
    seg_from, song_at = marker, start
    while song_at < end - 1e-6:
        seg_to = le if seg_from < le else seg_from + (le - ls)
        length = min(seg_to - seg_from, end - song_at)
        for n in notes:
            if seg_from - 1e-6 <= n["start"] < seg_from + length - 1e-6:
                out.append({**n, "start": round(song_at + n["start"] - seg_from, 4)})
        song_at += length
        seg_from = ls
    return sorted(out, key=lambda n: (n["start"], n["pitch"]))


def drum_voice(name: str | None, pitch: int) -> str:
    if not name:
        return GM_DRUMS.get(pitch, "other")
    s = name.lower()
    if "china" in s:
        return "china"
    if "crash" in s or "splash" in s:
        return "crash"
    if "ride" in s:
        return "ride_bell" if "bell" in s or "cup" in s else "ride"
    if "hat" in s or "hh" in s.split():
        if "open" in s:
            return "hihat_open"
        if "pedal" in s or "foot" in s:
            return "hihat_pedal"
        return "hihat"
    if "kick" in s or "bd" in s.split():
        return "kick"
    if "sidestick" in s or "side stick" in s or s.startswith("rim"):
        return "sidestick"
    if "snare" in s:
        return "snare"
    if "clap" in s:
        return "clap"
    if "tom" in s:
        return "tom"
    if any(w in s for w in ("cowbell", "conga", "tamb", "shaker", "bongo", "perc")):
        return "perc"
    return GM_DRUMS.get(pitch, "other")


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def fuzzy_rhythm_sim(a: frozenset, b: frozenset, steps: int | None = None) -> float:
    """Rhythm similarity that tolerates onsets moving by one 16th step, so parts that slowly
    drift against the grid (played live, or following a recording) still match bar to bar."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    def near(s, other):
        return any(abs(s - o) <= 1 or (steps and abs(s - o) >= steps - 1) for o in other)

    matched = sum(near(s, b) for s in a) + sum(near(s, a) for s in b)
    return matched / (len(a) + len(b))


def rhythm_changed(a: frozenset, b: frozenset, steps: int | None = None) -> bool:
    """A real rhythm change: onsets moved by more than a step, or their count jumped 1.5x."""
    if fuzzy_rhythm_sim(a, b, steps) < 0.6:
        return True
    lo, hi = sorted((len(a), len(b)))
    return hi >= 3 and hi >= 1.5 * max(1, lo)


def _letters(fps: list[frozenset], threshold: float, upper: bool) -> list[str]:
    reps: list[tuple[frozenset, str]] = []
    out = []
    for fp in fps:
        if not fp:
            out.append("-")
            continue
        best = max(((r_fp, letter) for r_fp, letter in reps),
                   key=lambda r: _jaccard(fp, r[0]), default=None)
        if best and _jaccard(fp, best[0]) >= threshold:
            out.append(best[1])
            continue
        i = len(reps)
        letter = chr(ord("A") + i % 26) + (str(i // 26 + 1) if i >= 26 else "")
        letter = letter if upper else letter.lower()
        reps.append((fp, letter))
        out.append(letter)
    return out


def _runs(values: list, first_bar: int) -> list[dict]:
    """Collapse consecutive equal values into {bars, value} runs."""
    out = []
    for i, v in enumerate(values):
        if out and out[-1]["value"] == v:
            out[-1]["bars"][1] = first_bar + i
        else:
            out.append({"bars": [first_bar + i, first_bar + i], "value": v})
    return out


def _repeats(letters: list[str], first_bar: int, limit: int = 6) -> list[dict]:
    found, covered = [], set()
    n = len(letters)
    for length in (8, 4, 2):
        for i in range(n - length + 1):
            seq = letters[i:i + length]
            if "-" in seq or len(set(seq)) == 1 or (i, length) in covered:
                continue
            hits, j = [], i + length
            while j <= n - length:
                if letters[j:j + length] == seq:
                    hits.append(j)
                    j += length
                else:
                    j += 1
            if hits and not any(i >= f["_i"] and i + length <= f["_i"] + f["_len"] for f in found):
                found.append({"_i": i, "_len": length,
                              "bars": [first_bar + i, first_bar + i + length - 1],
                              "pattern": "".join(seq),
                              "repeats_at": [first_bar + h for h in hits]})
                for h in hits:
                    covered.add((h, length))
            if len(found) >= limit:
                break
    for f in found:
        del f["_i"], f["_len"]
    return found


# ------------------------------------------------------------------ analysis

def grid_feel(onsets: list[float], tempo: float) -> dict:
    tol = max(0.02, 0.012 * tempo / 60)  # ~12 ms
    on_grid = {}
    for name, s in GRIDS:
        hits = sum(1 for o in onsets if abs(o - round(o / s) * s) <= tol)
        on_grid[name] = round(100 * hits / len(onsets))
    dominant = next((name for name, _ in GRIDS if on_grid[name] >= 90), None)
    s = dict(GRIDS).get(dominant or "1/16", 0.25)
    dev = mean(abs(o - round(o / s) * s) for o in onsets) * 60000 / tempo
    swing = {}
    for name, sub in (("1/8", 0.5), ("1/16", 0.25)):
        phases = [((o % (2 * sub)) / (2 * sub)) for o in onsets]
        off = [p for p in phases if 0.35 <= p <= 0.85]
        if len(off) >= 4:
            swing[name] = round(100 * mean(off), 1)
    return {"dominant": dominant or "unquantized", "on_grid_pct": on_grid,
            "mean_deviation_ms": round(dev, 1), "humanized": dev > 6,
            **({"swing_pct": swing} if swing else {})}


def _feel(snare_pos: list[float], bpb: float, next_bar_first: float | None = None) -> str:
    """Classify a bar from its accented snare hits (positions in beats from the bar start).

    Uses spacing between hits, not absolute beat positions, so parts that follow a
    drifting performance (off Live's grid) still read as backbeat / half-time.
    """
    if not snare_pos:
        return "no snare"
    hits = sorted(snare_pos)
    total = len(hits)
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    if total >= 5 and median(gaps) <= 0.5:
        return "busy/fill"
    if total == 1:
        # one hit: half-time unless the next snare comes back within half a bar
        if next_bar_first is None or bpb - hits[0] + next_bar_first >= 0.75 * bpb:
            return "half-time"
        return "syncopated"
    if gaps and all(abs(g - bpb / 2) <= 0.6 for g in gaps):
        return "backbeat"
    if total >= round(bpb) and gaps and all(abs(g - 1) <= 0.3 for g in gaps):
        return "every beat"
    return "syncopated"


def drums(notes: list[dict], pads: dict[int, str] | None, bpb: float, bars: list[int],
          first_bar: int) -> dict:
    steps = max(1, round(bpb / STEP))
    voice_of = {}
    by_voice: dict[str, list[dict]] = defaultdict(list)
    for n in notes:
        v = voice_of.setdefault(n["pitch"], drum_voice((pads or {}).get(n["pitch"]), n["pitch"]))
        by_voice[v].append(n)
    nbars = len(bars)
    voices = {}
    for v, ns in sorted(by_voice.items(), key=lambda kv: -len(kv[1])):
        per_bar: dict[int, set] = defaultdict(set)
        for n in ns:
            b = bar_of(n["start"], bpb)
            per_bar[b].add(min(steps - 1, round((n["start"] - bar_start(b, bpb)) / STEP)))
        patterns = Counter("".join("x" if i in s else "." for i in range(steps))
                           for s in per_bar.values())
        common, count = patterns.most_common(1)[0]
        voices[v] = {"hits": len(ns), "per_bar": round(len(ns) / max(1, nbars), 1),
                     "common_pattern": common, "pattern_bars": count,
                     "velocity": round(mean(n["velocity"] for n in ns)),
                     "pads": sorted({(pads or {}).get(n["pitch"]) or str(n["pitch"]) for n in ns})}
    # per-bar feel and timekeeper, collapsed into runs
    feel, keeper = [], []
    accented = sorted(n["start"] for n in by_voice.get("snare", []) + by_voice.get("clap", [])
                      if n["velocity"] >= 60)
    for b in bars:
        lo, hi = bar_start(b, bpb), bar_start(b + 1, bpb)
        sn = [t - lo for t in accented if lo <= t < hi]
        nxt = next((t - hi for t in accented if hi <= t < hi + bpb), None)
        feel.append(_feel(sn, bpb, nxt) if any(lo <= n["start"] < hi for n in notes) else "empty")
        counts = {v: sum(1 for n in by_voice.get(v, []) if lo <= n["start"] < hi) for v in TIMEKEEPERS}
        top = max(counts, key=counts.get)
        keeper.append(top if counts[top] else "none")
    kicks = sorted(n["start"] for n in by_voice.get("kick", []))
    runs, run = 0, 1
    for a, b in zip(kicks, kicks[1:]):
        if b - a <= 0.26:
            run += 1
        else:
            runs += run >= 4
            run = 1
    runs += run >= 4
    sections = []
    for i, b in enumerate(bars):
        key = (feel[i], keeper[i])
        if sections and sections[-1]["_key"] == key and sections[-1]["bars"][1] == b - 1:
            sections[-1]["bars"][1] = b
        else:
            sections.append({"_key": key, "bars": [b, b], "feel": key[0], "timekeeper": key[1]})
    for s in sections:
        del s["_key"]
    return {"voices": voices, "sections": sections, "double_kick_runs": runs,
            "step": "1/16", "steps_per_bar": steps}


def analyze_clip(notes: list[dict], bpb: float, tempo: float, *, pads: dict[int, str] | None = None,
                 is_drums: bool = False, start_bar: int | None = None, end_bar: int | None = None,
                 include_notes: bool = False) -> dict:
    if start_bar is not None:
        notes = [n for n in notes if n["start"] >= bar_start(start_bar, bpb) - 1e-6]
    if end_bar is not None:
        notes = [n for n in notes if n["start"] < bar_start(end_bar + 1, bpb) - 1e-6]
    if not notes:
        return {"notes": 0, "note": "no notes in range"}
    notes = sorted(notes, key=lambda n: (n["start"], n["pitch"]))
    first = start_bar or bar_of(notes[0]["start"], bpb)
    last = end_bar or bar_of(max(n["start"] for n in notes), bpb)
    bars = list(range(first, last + 1))
    steps = max(1, round(bpb / STEP))

    by_bar: dict[int, list[dict]] = defaultdict(list)
    for n in notes:
        by_bar[bar_of(n["start"], bpb)].append(n)
    onsets = sorted({round(n["start"], 4) for n in notes})
    groups = Counter(round(n["start"], 3) for n in notes)
    sounding = []
    for n in notes:
        sounding.append(sum(1 for m in notes
                            if m["start"] <= n["start"] < m["start"] + m["duration"] - 1e-6))

    vel = [n["velocity"] for n in notes]
    step_vel: dict[int, list[float]] = defaultdict(list)
    fp_pitch, fp_rhythm = [], []
    for b in bars:
        ns = by_bar.get(b, [])
        ss = [(min(steps - 1, round((n["start"] - bar_start(b, bpb)) / STEP)), n) for n in ns]
        for s, n in ss:
            step_vel[s].append(n["velocity"])
        fp_pitch.append(frozenset((s, n["pitch"]) for s, n in ss))
        fp_rhythm.append(frozenset(s for s, _ in ss))

    def accent(s):
        if s not in step_vel:
            return "."
        v = mean(step_vel[s])
        return "-" if v < 50 else "o" if v < 80 else "O" if v < 105 else "X"

    letters = _letters(fp_pitch, 0.8, upper=True)
    rletters = _letters(fp_rhythm, 0.8, upper=False)
    patterns = Counter("".join("x" if i in fp else "." for i in range(steps))
                       for fp in fp_rhythm if fp)

    # phrase boundaries: big change vs previous bar, or entry after silence
    boundaries, last_b = [0], 0
    for i in range(1, len(bars)):
        if letters[i] == "-":
            continue
        prev_empty = letters[i - 1] == "-"
        novelty = 1 - _jaccard(fp_pitch[i], fp_pitch[i - 1])
        if (prev_empty or novelty >= 0.6) and i - last_b >= 2:
            boundaries.append(i)
            last_b = i
    phrases = []
    for j, bi in enumerate(boundaries):
        end_i = (boundaries[j + 1] if j + 1 < len(boundaries) else len(bars)) - 1
        phrases.append({"bars": [bars[bi], bars[end_i]], "form": "".join(letters[bi:end_i + 1])})

    changes, prev = [], None
    for i, b in enumerate(bars):
        if rletters[i] == "-":
            continue
        if prev is not None and rletters[i] != rletters[prev] and \
                rhythm_changed(fp_rhythm[i], fp_rhythm[prev], steps):
            changes.append({"bar": b, "from": rletters[prev], "to": rletters[i],
                            "onsets": [len(fp_rhythm[prev]), len(fp_rhythm[i])]})
        prev = i

    per_bar_counts = [len(by_bar.get(b, [])) for b in bars]
    out = {
        "notes": len(notes),
        "bars": [first, last],
        "density": {"avg_notes_per_bar": round(mean(per_bar_counts), 1),
                    "max_notes_per_bar": max(per_bar_counts),
                    "per_bar": per_bar_counts if len(bars) <= 128 else "omitted (>128 bars)"},
        "velocity": {"min": round(min(vel)), "max": round(max(vel)), "mean": round(mean(vel)),
                     "stdev": round(pstdev(vel), 1),
                     "dynamics": "flat" if pstdev(vel) < 6 else "moderate" if pstdev(vel) < 15 else "wide",
                     "accent_map": "".join(accent(s) for s in range(steps)),
                     "accent_legend": "per 16th step: . none, - <50, o <80, O <105, X loud"},
        "grid": grid_feel(onsets, tempo),
        "rhythm": {"steps_per_bar": steps,
                   "common_patterns": [{"pattern": p, "bars": c} for p, c in patterns.most_common(3)]},
        "form": {"first_bar": first,
                 "bars": "".join(letters),
                 "rhythm_only": "".join(rletters),
                 "legend": "one letter per bar; same letter = same notes (rhythm_only: same rhythm); - = empty",
                 "runs": [r for r in _runs(letters, first) if r["value"] != "-" and r["bars"][1] > r["bars"][0]][:12],
                 "repeats": _repeats(letters, first),
                 "phrases": phrases[:24],
                 "rhythm_changes": changes[:16]},
    }
    if out["grid"]["dominant"] == "unquantized" and out["grid"]["mean_deviation_ms"] > 20:
        out["form"]["note"] = ("notes sit well off Live's grid (played live, or following a "
                               "recording), so bar letters split repeats that are musically the "
                               "same; rely on drums.sections, density and rhythm_changes instead")
    if is_drums or pads:
        out["drums"] = drums(notes, pads, bpb, bars, first)
    else:
        out["pitch"] = {"range": [music.note_name(min(n["pitch"] for n in notes)),
                                  music.note_name(max(n["pitch"] for n in notes))],
                        "median": music.note_name(int(median(n["pitch"] for n in notes))),
                        "distinct": len({n["pitch"] for n in notes}),
                        "most_used": [music.note_name(p) for p, _ in
                                      Counter(n["pitch"] for n in notes).most_common(5)]}
        out["polyphony"] = {"max": max(sounding), "avg_notes_per_onset": round(mean(groups.values()), 2)}
        harmony = music.analyze(notes, bpb)
        out["harmony"] = {"key_candidates": harmony.get("key_candidates", []),
                          "bars": [{"bar": b["bar"], "lowest": b["lowest_note"],
                                    "pitch_classes": b["pitch_classes"][:5]}
                                   for b in harmony.get("bars", [])][:32]}
    if include_notes:
        out["note_list"] = [{k: round(n[k], 4) if isinstance(n[k], float) else n[k]
                             for k in ("pitch", "start", "duration", "velocity")} for n in notes]
    return out
