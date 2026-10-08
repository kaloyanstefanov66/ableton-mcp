"""Arrangement structure: who plays where, sections, repeats, empty regions, rhythm changes.

Everything works on a bar grid. Inputs are already in song time:
  tracks          [{id, name, kind, role, clips: [{start, end, kind, muted}]}]
  notes_by_track  {track_id: [{pitch, start, duration, velocity}]}  (MIDI tracks)
  audio_by_track  {track_id: {bar: {rms_db, onsets}}}                 (only when analyzed)
  locators        [{name, time}]
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from .midi_analysis import STEP, _jaccard, _letters
from . import refs
from .refs import bar_of, bar_start

ACTIVE_RMS_DB = -50.0
MIN_SECTION_BARS = 4
WINDOW = 2


def _coverage(clips: list[dict], lo: float, hi: float) -> float:
    covered = 0.0
    for c in clips:
        if c.get("muted"):
            continue
        covered += max(0.0, min(hi, c["end"]) - max(lo, c["start"]))
    return min(1.0, covered / (hi - lo))


def _runs(bars: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for b in bars:
        if out and out[-1][1] == b - 1:
            out[-1][1] = b
        else:
            out.append([b, b])
    return out


def bar_grid(tracks, notes_by_track, audio_by_track, bpb, first, last):
    """Per track per bar: coverage, notes, rhythm/pitch fingerprints, audio stats, active."""
    steps = max(1, round(bpb / STEP))
    grid = {}
    for t in tracks:
        tid = t["id"]
        notes = notes_by_track.get(tid, [])
        by_bar = defaultdict(list)
        for n in notes:
            by_bar[bar_of(n["start"], bpb)].append(n)
        audio = audio_by_track.get(tid)
        rows = {}
        for b in range(first, last + 1):
            lo, hi = bar_start(b, bpb), bar_start(b + 1, bpb)
            cov = _coverage(t["clips"], lo, hi)
            ns = by_bar.get(b, [])
            rel = [(min(steps - 1, round((n["start"] - lo) / STEP)), n) for n in ns]
            a = (audio or {}).get(b)
            if t["kind"] == "midi":
                active = bool(ns)
            else:
                active = cov >= 0.25 and (a is None or a["rms_db"] > ACTIVE_RMS_DB)
            rows[b] = {"cov": cov, "notes": len(ns), "active": active,
                       "rhythm": frozenset(s for s, _ in rel),
                       "pitch": frozenset((s, n["pitch"]) for s, n in rel),
                       "rms_db": a["rms_db"] if a else None,
                       "onsets": a["onsets"] if a else None}
        grid[tid] = rows
    return grid


def _density(row: dict, peak: float) -> float:
    v = row["notes"] if row["onsets"] is None else row["onsets"]
    return min(1.0, v / peak) if peak else float(row["active"])


def _boundaries(tracks, grid, bars, bpb):
    peaks = {}
    for t in tracks:
        rows = grid[t["id"]]
        peaks[t["id"]] = max((r["notes"] if r["onsets"] is None else r["onsets"]) for r in rows.values()) or 0
    vec = {b: [x for t in tracks for x in (float(grid[t["id"]][b]["active"]),
                                           _density(grid[t["id"]][b], peaks[t["id"]]))]
           for b in bars}
    n = max(1, len(tracks))
    scores = {}
    for i, b in enumerate(bars[1:], start=1):
        left = [vec[x] for x in bars[max(0, i - WINDOW):i]]
        right = [vec[x] for x in bars[i:i + WINDOW]]
        lm = [mean(c) for c in zip(*left)]
        rm = [mean(c) for c in zip(*right)]
        score = sum(abs(x - y) for x, y in zip(lm, rm)) / (2 * n)
        edge = bar_start(b, bpb)
        clip_edges = sum(1 for t in tracks for c in t["clips"]
                         if not c.get("muted") and (abs(c["start"] - edge) < 0.05 or abs(c["end"] - edge) < 0.05))
        scores[b] = score + min(0.45, 0.15 * clip_edges)
    chosen = [bars[0]]
    for b, s in sorted(scores.items(), key=lambda kv: -kv[1]):
        if s < 0.25:
            break
        gap = min(abs(b - c) for c in chosen + [bars[-1] + 1])
        if gap >= MIN_SECTION_BARS or (s >= 0.6 and gap >= 2):
            chosen.append(b)
    return sorted(chosen), scores


def _section_similarity(a, b, tracks, grid, letters):
    sims = []
    for t in tracks:
        tid = t["id"]
        ra = [grid[tid][x] for x in range(a[0], a[1] + 1)]
        rb = [grid[tid][x] for x in range(b[0], b[1] + 1)]
        act_a = mean(r["active"] for r in ra)
        act_b = mean(r["active"] for r in rb)
        if act_a < 0.1 and act_b < 0.1:
            sims.append(1.0)
            continue
        sim = 1 - abs(act_a - act_b)
        if tid in letters:
            la = {letters[tid][x] for x in range(a[0], a[1] + 1)} - {"-"}
            lb = {letters[tid][x] for x in range(b[0], b[1] + 1)} - {"-"}
            sim = (sim + _jaccard(frozenset(la), frozenset(lb))) / 2
        sims.append(sim)
    length = min(a[1] - a[0], b[1] - b[0]) + 1, max(a[1] - a[0], b[1] - b[0]) + 1
    return mean(sims) * (0.8 + 0.2 * length[0] / length[1])


def _rhythm_changes(t, rows, bars, letters_r):
    out, prev = [], None
    for b in bars:
        r = rows[b]
        if not r["active"]:
            continue
        if t["kind"] == "midi":
            if prev is not None and letters_r[b] != letters_r[prev] and \
                    _jaccard(r["rhythm"], rows[prev]["rhythm"]) < 0.6:
                out.append({"bar": b, "onsets_per_bar": [len(rows[prev]["rhythm"]), len(r["rhythm"])]})
        elif r["onsets"] is not None and prev is not None and rows[prev]["onsets"] is not None:
            a0, a1 = rows[prev]["onsets"], r["onsets"]
            if max(a0, a1) >= 3 and (a1 >= 1.6 * max(1, a0) or a1 <= 0.6 * a0) and abs(a1 - a0) >= 3:
                out.append({"bar": b, "onsets_per_bar": [a0, a1]})
        prev = b
    return out[:16]


def arrangement_map(tracks, notes_by_track, audio_by_track, locators, bpb,
                    start_bar=None, end_bar=None) -> dict:
    tracks = [t for t in tracks if t["kind"] in ("midi", "audio")]
    spans = [c for t in tracks for c in t["clips"] if not c.get("muted")]
    if not spans:
        return {"sections": [], "note": "no arrangement clips"}
    first = start_bar or bar_of(min(c["start"] for c in spans), bpb)
    last = end_bar or refs.end_bar(max(c["end"] for c in spans), bpb)
    bars = list(range(first, last + 1))
    grid = bar_grid(tracks, notes_by_track, audio_by_track, bpb, first, last)

    letters, letters_r = {}, {}
    for t in tracks:
        if t["kind"] == "midi":
            rows = grid[t["id"]]
            letters[t["id"]] = dict(zip(bars, _letters([rows[b]["pitch"] for b in bars], 0.8, True)))
            letters_r[t["id"]] = dict(zip(bars, _letters([rows[b]["rhythm"] for b in bars], 0.8, False)))

    locs = [l for l in locators if first <= bar_of(l["time"], bpb) <= last]
    if locs:
        starts = sorted({first} | {bar_of(l["time"], bpb) for l in locs})
        names = {bar_of(l["time"], bpb): l["name"] for l in locs}
        source = "locators"
    else:
        starts, _ = _boundaries(tracks, grid, bars, bpb)
        names = {}
        source = "inferred from activity, density and clip boundaries"
    spans_s = [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else last) for i, s in enumerate(starts)]

    labels, reps = [], []
    for span in spans_s:
        best = max(((_section_similarity(span, r_span, tracks, grid, letters), lab)
                    for r_span, lab in reps), default=(0.0, None))
        if best[1] and best[0] >= 0.85:
            labels.append(best[1])
        elif best[1] and best[0] >= 0.7:
            labels.append(best[1] + "'")
        else:
            lab = chr(ord("A") + len(reps) % 26)
            reps.append((span, lab))
            labels.append(lab)

    sections = []
    for (s, e), lab in zip(spans_s, labels):
        active, silent = [], []
        for t in tracks:
            rows = [grid[t["id"]][b] for b in range(s, e + 1)]
            ratio = mean(r["active"] for r in rows)
            if ratio >= 0.25:
                entry = {"track": t["id"], "name": t["name"], "role": t["role"],
                         "active_pct": round(100 * ratio)}
                if t["kind"] == "midi":
                    entry["notes_per_bar"] = round(mean(r["notes"] for r in rows), 1)
                elif rows[0]["onsets"] is not None:
                    entry["onsets_per_bar"] = round(mean(r["onsets"] for r in rows), 1)
                    rms = [r["rms_db"] for r in rows if r["rms_db"] is not None]
                    if rms:
                        entry["rms_db"] = round(mean(rms), 1)
                active.append(entry)
            else:
                silent.append(t["id"])
        sec = {"label": lab, "bars": [s, e], "length_bars": e - s + 1,
               "active": active, "roles": sorted({a["role"] for a in active}),
               "silent_tracks": silent}
        if s in names:
            sec["name"] = names[s]
        sections.append(sec)

    empty = [b for b in bars if not any(grid[t["id"]][b]["active"] for t in tracks)]
    activity = {}
    for t in tracks:
        rows = grid[t["id"]]
        activity[t["id"]] = "".join("#" if rows[b]["active"] else "~" if rows[b]["cov"] > 0 else "."
                                    for b in bars)
    out = {
        "range_bars": [first, last],
        "sections_source": source,
        "form": " ".join(labels),
        "sections": sections,
        "activity": {"first_bar": first, "legend": "# active, ~ clip but silent/empty, . nothing",
                     "tracks": {f"{t['id']} {t['name']}": activity[t["id"]] for t in tracks}},
        "empty_bars": _runs(empty),
        "rhythm_changes": {t["id"]: ch for t in tracks
                           if (ch := _rhythm_changes(t, grid[t["id"]], bars, letters_r.get(t["id"], {})))},
    }
    if start_bar is not None or end_bar is not None:
        out["in_range"] = {
            "active": [{"track": t["id"], "name": t["name"], "role": t["role"],
                        "active_bars": _runs([b for b in bars if grid[t["id"]][b]["active"]])}
                       for t in tracks if any(grid[t["id"]][b]["active"] for b in bars)],
            "silent": [t["id"] for t in tracks if not any(grid[t["id"]][b]["active"] for b in bars)],
        }
    return out
