"""Shape raw Remote Script snapshots into concise, deterministic views for Claude.

Rules: stable IDs next to human names, times as bars and 'bar.beat.sixteenth' positions,
omit defaults and empty collections, never dump whole parameter lists unless asked.
"""
from __future__ import annotations

import re

from .refs import bar_of, beats_per_bar, end_bar, pos

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Matched against word prefixes of the track name first, then device names/classes.
ROLE_WORDS = [
    ("drums", ("drum", "kit", "beat", "perc", "kick", "snare", "hat", "cymbal", "808", "909")),
    ("bass", ("bass", "sub")),
    ("guitar", ("guitar", "gtr", "git", "amp", "cab", "riff", "distortion", "overdrive", "fuzz")),
    ("vocals", ("vox", "vocal", "voice", "sing", "choir")),
    ("keys", ("piano", "keys", "rhodes", "organ", "synth", "pad", "lead", "drift", "wavetable",
              "operator", "analog", "string")),
    ("fx", ("fx", "sfx", "riser", "impact", "noise", "sweep")),
]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def guess_role(track: dict) -> str:
    """drums/bass/guitar/vocals/keys/fx/unknown from track name, then devices."""
    kind = track.get("kind")
    if kind in ("return", "master"):
        return kind
    devices = track.get("devices", [])
    if any(d.get("rack") == "drum_rack" for d in devices):
        return "drums"
    for text in [track.get("name", "")] + [f"{d.get('name', '')} {d.get('class', '')}" for d in devices]:
        words = _words(text)
        for role, keys in ROLE_WORDS:
            if any(w.startswith(k) for w in words for k in keys):
                return role
    return "unknown"


def _span(clip: dict, bpb: float) -> list[int] | None:
    if clip.get("start") is None:
        return None
    return [bar_of(clip["start"], bpb), end_bar(max(clip["start"] + 1e-3, clip["end"]), bpb)]


def _key(song: dict) -> str | None:
    k = song.get("key")
    return f"{NAMES[k['root'] % 12]} {k['scale']}" if k else None


def _flags(t: dict) -> dict:
    return {f: True for f in ("mute", "solo", "arm") if t.get(f)}


def _sends(mixer: dict) -> dict:
    return {k: v for k, v in (mixer.get("sends") or {}).items() if "inf" not in v}


def _routing(t: dict) -> str | None:
    r = t.get("routing") or {}
    if not r:
        return None
    return f"{r.get('input', '-')} -> {r.get('output', '-')}"


def _device_label(d: dict) -> str:
    label = d["name"]
    if d.get("class") and d["class"] != d["name"]:
        label += f" [{d['class']}]"
    if d.get("on") is False:
        label += " (off)"
    return label


def _clip_line(c: dict, bpb: float) -> dict:
    out = {"id": c["id"], "name": c.get("name") or "", "kind": c["kind"]}
    span = _span(c, bpb)
    if span:
        out["bars"] = span
        out["position"] = pos(c["start"], bpb)
    else:
        out["length_bars"] = round(c["length"] / bpb, 2)
    for k in ("looping", "muted"):
        if c.get(k):
            out[k] = True
    return out


def track_summary(t: dict, bpb: float, detail: str) -> dict:
    out = {"id": t["id"], "name": t["name"], "kind": t["kind"], "role": guess_role(t)}
    if t.get("group"):
        out["group"] = t["group"]
    out.update(_flags(t))
    mixer = t.get("mixer") or {}
    out["volume"] = mixer.get("volume")
    if mixer.get("pan") not in (None, "C"):
        out["pan"] = mixer["pan"]
    sends = _sends(mixer)
    if sends:
        out["sends"] = sends
    routing = _routing(t)
    if routing and detail == "full":
        out["routing"] = routing
    if t.get("devices"):
        out["devices"] = ([{"id": d["id"], "label": _device_label(d)} for d in t["devices"]]
                          if detail == "full" else [_device_label(d) for d in t["devices"]])
    session = t.get("session_clips") or []
    arrangement = t.get("arrangement_clips") or []
    if detail == "full":
        if session:
            out["session_clips"] = [_clip_line(c, bpb) for c in session]
        if arrangement:
            out["arrangement_clips"] = [_clip_line(c, bpb) for c in arrangement]
    else:
        clips = {}
        if session:
            clips["session"] = len(session)
        if arrangement:
            clips["arrangement"] = len(arrangement)
            spans = [_span(c, bpb) for c in arrangement]
            clips["bars"] = [min(s[0] for s in spans), max(s[1] for s in spans)]
        if clips:
            out["clips"] = clips
    if t.get("playing_slot") is not None:
        out["playing_slot"] = t["playing_slot"]
    return out


def project(snap: dict, detail: str = "summary") -> dict:
    s = snap["song"]
    bpb = beats_per_bar(s["signature"])
    song = {
        "live_version": snap.get("live_version"),
        "tempo": s["tempo"],
        "time_signature": "%d/%d" % tuple(s["signature"]),
        "key": _key(s),
        "length_bars": end_bar(max(1e-3, s.get("song_length") or 0), bpb),
        "playing": s.get("is_playing", False),
        "position": pos(s.get("position") or 0.0, bpb),
    }
    if s["loop"]["on"]:
        song["loop_bars"] = [bar_of(s["loop"]["start"], bpb),
                             end_bar(s["loop"]["start"] + s["loop"]["length"], bpb)]
    song = {k: v for k, v in song.items() if v is not None}
    tracks = [t for t in snap["tracks"] if t["kind"] not in ("return", "master")]
    returns = [t for t in snap["tracks"] if t["kind"] == "return"]
    master = next((t for t in snap["tracks"] if t["kind"] == "master"), None)
    scenes = [sc for sc in s.get("scenes", []) if sc["clips"] or sc.get("name") or sc.get("tempo")]
    out = {
        "song": song,
        "locators": [{"name": c["name"], "bar": bar_of(c["time"], bpb), "position": pos(c["time"], bpb)}
                     for c in s.get("locators", [])],
        "scenes": {"count": len(s.get("scenes", [])),
                   "used": [{k: v for k, v in sc.items() if v not in ("", None)} for sc in scenes]},
        "tracks": [track_summary(t, bpb, detail) for t in tracks],
        "returns": [track_summary(t, bpb, detail) for t in returns],
        "master": track_summary(master, bpb, detail) if master else None,
        "selection": {"track": snap.get("selected_track"), "clip": snap.get("detail_clip")},
    }
    if not out["locators"]:
        out["locators_note"] = "no locators - get_arrangement_map infers sections instead"
    return out


def track_state(raw: dict, bpb: float) -> dict:
    out = track_summary({**raw, "mixer": {}}, bpb, "full")
    out.pop("volume", None)
    out["mixer"] = {p["name"]: {"id": p["id"], "display": p["display"],
                                **({"automation": p["automation"]} if p.get("automation") else {})}
                    for p in raw.get("mixer", [])}
    if raw.get("routing"):
        out["routing"] = raw["routing"]
    out["devices"] = raw.get("devices", [])
    if raw.get("automated_params"):
        out["automated_params"] = raw["automated_params"]
    out["automation_note"] = ("arrangement automation is detected per parameter but its points "
                              "are not readable through Live's API; clip envelopes are, via get_clip_state")
    return out


def device_state(raw: dict, params: str = "changed", limit: int = 120) -> dict:
    allp = raw.get("parameters", [])
    mode = (params or "changed").lower()
    if raw.get("type") == "mixer" or mode == "all":
        chosen = allp
    elif mode == "automated":
        chosen = [p for p in allp if p.get("automation")]
    elif mode == "changed":
        chosen = [p for i, p in enumerate(allp)
                  if i == 0 or p.get("changed") or p.get("automation") or p.get("options")
                  or p.get("name", "").lower().startswith("macro")]
    else:
        chosen = [p for p in allp if mode in (p.get("name") or "").lower()]
    out = {k: v for k, v in raw.items() if k != "parameters"}
    slim = []
    for p in chosen[:limit]:
        q = {k: p[k] for k in ("id", "name", "display") if k in p}
        for k in ("automation", "enabled"):
            if k in p:
                q[k] = p[k]
        if mode != "changed" or p.get("options"):
            for k in ("value", "min", "max", "options", "default"):
                if k in p:
                    q[k] = p[k]
        slim.append(q)
    out["parameters"] = slim
    out["parameters_shown"] = f"{len(slim)} of {len(allp)} ({mode})"
    return out


def clip_state(raw: dict, bpb: float) -> dict:
    out = dict(raw)
    if raw.get("start_time") is not None:
        out["bars"] = [bar_of(raw["start_time"], bpb), end_bar(raw["end_time"], bpb)]
        out["position"] = pos(raw["start_time"], bpb)
    for k in ("start", "end"):
        out.pop(k, None)
    if not out.get("envelopes"):
        out.pop("envelopes", None)
    return out
