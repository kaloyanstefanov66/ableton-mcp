"""MCP server exposing Ableton Live (incl. Lite) to Claude."""
from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import audio_analysis, midi_analysis, music, refs, structure, views
from .connection import AbletonConnection, AbletonError
from .journal import JournaledConnection
from .transcribe import transcribe_seconds
from .worker import run_jobs

EXPORT_DIR = Path(__file__).resolve().parents[2] / "exports"

INSTRUCTIONS = """\
Understands and edits the user's Ableton Live 12 **Lite** set through a Remote Script.

Understand before editing
- get_project_state: the whole set (tempo, key, locators, tracks with role/mixer/devices).
- get_arrangement_map: sections (A/B/A'), who plays where, empty bars, rhythm changes.
  Pass start_bar/end_bar for "what happens in bars 33-49"; analyze_audio=true to look inside audio.
- analyze_midi_clip / analyze_audio_clip: musical content of one clip (grid, feel, form,
  phrases, drum patterns / levels, onsets, timing drift, spectrum).
- get_track_state, get_device_state, get_clip_state: mixer, routing, device chains,
  parameters, clip envelopes.

IDs: t3 track, r0 return, m master; t3/s2 session clip, t3/a0 arrangement clip; t3/d1 device,
t3/mx mixer, <device>/p5 parameter. IDs are positional: re-read after adding/removing things.
Legacy tools take ints (track, slot, arrangement_index) - t3/a0 = track 3, arrangement_index 0.

Conventions
- Times are in beats (quarter notes); bars are 1-based; positions are 'bar.beat.sixteenth'.
- Note names follow Ableton: MIDI 60 = C3. Drum pads: read real pad names from
  get_track_state / analyze_midi_clip instead of assuming GM (36 kick, 38 snare, 42 hat ...).

Writing
- Wrap related edits in edit_session(begin) ... preview ... commit/rollback.
- If the reference part is a human recording that drifts off the grid, analyze_audio_clip
  (timing) shows it; write notes on the grid and pass follow_timing=<that audio clip id> so
  the new part locks to the performance.
- Polyphonic guitar transcription is approximate: trust onsets, lowest notes and per-bar
  pitch classes more than individual notes.

Lite limits: capped track/scene counts, no Max for Live, a small instrument library. If a
create call fails because of a limit, tell the user rather than retrying.
"""

mcp = MCPServer("ableton", instructions=INSTRUCTIONS)
live = JournaledConnection(AbletonConnection())


def tool(fn):
    """Register a tool. Expected failures reach Claude with their message; the SDK would
    otherwise replace anything that isn't a ToolError with a generic error."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (AbletonError, ValueError, IndexError, OSError, RuntimeError) as e:
            raise ToolError(str(e)) from e

    return mcp.tool()(wrapper)


class Note(BaseModel):
    pitch: int = Field(ge=0, le=127, description="MIDI pitch (60 = C3 in Ableton)")
    start: float = Field(ge=0, description="start in beats from the clip start")
    duration: float = Field(gt=0, description="length in beats")
    velocity: int = Field(default=100, ge=1, le=127)


def _ref(track: int, slot: int | None, arrangement_index: int | None) -> dict:
    if slot is None and arrangement_index is None:
        raise ValueError("pass slot (Session View) or arrangement_index (Arrangement View)")
    return {"track": track, "slot": slot, "arrangement_index": arrangement_index}


def _beats_per_bar() -> float:
    return refs.beats_per_bar(live.call("song_state")["signature"])


@dataclass
class ClipTime:
    """Maps between an audio clip's region (beats from region start), its source file
    (seconds) and the bar grid (song bars for Arrangement clips, clip bars for Session clips)."""
    clip: dict
    ref: dict
    region: tuple[float, float]           # clip beats
    region_s: tuple[float, float]         # file seconds
    to_beats: Callable[[float], float]    # file seconds -> region beats
    to_seconds: Callable[[float], float]  # region beats -> file seconds
    song_start: float | None              # arrangement start time, None for session clips

    @property
    def length(self) -> float:
        return self.region[1] - self.region[0]

    def origin(self) -> float:
        """Region beat 0 in the pulse/bar coordinate system."""
        return self.song_start or 0.0

    def bar(self, rel_beat: float, bpb: float) -> int:
        return refs.bar_of(self.origin() + rel_beat, bpb)


def _clip_time(ref: dict, start_beat: float | None = None, end_beat: float | None = None) -> ClipTime:
    clip = live.call("get_clip", **ref)
    if clip["is_midi"]:
        raise ValueError("that is a MIDI clip - use analyze_midi_clip / get_clip_notes")
    if start_beat is None or end_beat is None:
        if clip["looping"]:
            a, b = clip["loop_start"], clip["loop_end"]
        elif "start_time" in clip:
            a = clip["start_marker"]
            b = a + (clip["end_time"] - clip["start_time"])
        else:
            a, b = clip["start_marker"], clip["end_marker"]
        start_beat = a if start_beat is None else start_beat
        end_beat = b if end_beat is None else end_beat
    if end_beat <= start_beat:
        raise ValueError("end_beat must be after start_beat")
    tempo = clip["song_tempo"]
    if clip["warping"]:
        step = max(0.25, (end_beat - start_beat) / 2000)
        grid = [start_beat]
        while grid[-1] + step < end_beat:
            grid.append(grid[-1] + step)
        grid.append(end_beat)
        times = live.call("audio_beat_to_seconds", beats=grid, **ref)
        duration = clip.get("sample_length", 0) / clip.get("sample_rate", 1) or None
        if duration and max(times) > duration * 1.5:  # Live answered in samples
            times = [t / clip["sample_rate"] for t in times]
        mapping = music.PiecewiseLinear(grid, times)
        a0 = start_beat
        region_s = (mapping.forward(start_beat), mapping.forward(end_beat))
        to_beats = lambda s: mapping.inverse(s) - a0  # noqa: E731
        to_seconds = lambda rel: mapping.forward(a0 + rel)  # noqa: E731
    else:
        # Unwarped clips keep their markers in seconds and play at the file's own speed.
        region_s = (start_beat, end_beat)
        to_beats = lambda s: (s - region_s[0]) * tempo / 60  # noqa: E731
        to_seconds = lambda rel: region_s[0] + rel * 60 / tempo  # noqa: E731
    return ClipTime(clip, ref, (start_beat, end_beat), region_s, to_beats, to_seconds,
                    clip.get("start_time"))


def _audio_features(ct: ClipTime, bpb: float) -> tuple[dict, list[int]]:
    """Per-bar features + onsets for an audio clip (cached by the worker)."""
    first, last = ct.bar(0.0, bpb), ct.bar(max(0.0, ct.length - 1e-3), bpb)
    bars, segments = [], []
    for b in range(first, last + 1):
        lo = max(0.0, refs.bar_start(b, bpb) - ct.origin())
        hi = min(ct.length, refs.bar_start(b + 1, bpb) - ct.origin())
        if hi > lo:
            bars.append(b)
            segments.append([round(ct.to_seconds(lo), 4), round(ct.to_seconds(hi), 4)])
    job = {"type": "features", "audio_path": ct.clip["file_path"], "start_s": ct.region_s[0],
           "end_s": ct.region_s[1], "segments_s": segments}
    return run_jobs([job])[0], bars


def _pulse(ct: ClipTime, bpb: float) -> audio_analysis.Pulse | None:
    feats, _ = _audio_features(ct, bpb)
    onsets = [ct.origin() + ct.to_beats(s) for s in feats["onsets_s"]]
    return audio_analysis.estimate_pulse(onsets, ct.clip["song_tempo"])


def _follow(notes: list[dict], follow_clip: str, target_song_start: float | None) -> tuple[list[dict], dict]:
    """Move grid-written notes onto the pulse of a recorded performance."""
    bpb = _beats_per_bar()
    ct = _clip_time(refs.parse_clip(follow_clip))
    pulse = _pulse(ct, bpb)
    if pulse is None or pulse.confidence < audio_analysis.FOLLOW_MIN_CONFIDENCE:
        conf = pulse.confidence if pulse else 0
        raise ValueError(f"can't follow {follow_clip}: pulse confidence {conf} (< "
                         f"{audio_analysis.FOLLOW_MIN_CONFIDENCE}). Check analyze_audio_clip("
                         f"'{follow_clip}', ['timing']) or write on the grid instead.")
    base = (target_song_start if target_song_start is not None else ct.song_start) \
        if ct.song_start is not None else 0.0
    out = []
    for n in notes:
        s = pulse.to_actual(base + n["start"]) - base
        e = pulse.to_actual(base + n["start"] + n["duration"]) - base
        s = max(0.0, s)
        out.append({**n, "start": round(s, 4), "duration": round(max(0.01, e - s), 4)})
    return out, {"followed": follow_clip, "performed_bpm": round(pulse.performed_bpm, 1),
                 "pulse": pulse.tick_label, "confidence": pulse.confidence}


# ------------------------------------------------------------------ reading


@tool
def get_session() -> dict:
    """Lightweight overview (legacy int indices): tempo, tracks, devices, clips, selection.
    Prefer get_project_state for mixer/routing/locators/roles and stable IDs."""
    return live.call("get_session")


@tool
def get_track(track: int) -> dict:
    """Clips of one track with loop/marker positions (legacy int index).
    Prefer get_track_state for mixer, routing, device chains and automation."""
    return live.call("get_track", track=track)


@tool
def get_clip_notes(track: int, slot: int | None = None, arrangement_index: int | None = None,
                   analyze: bool = True) -> dict:
    """Read the notes of a MIDI clip, plus an analysis (key, per-bar harmony, rhythm)."""
    result = live.call("get_notes", **_ref(track, slot, arrangement_index))
    if analyze:
        result["analysis"] = music.analyze(result["notes"], _beats_per_bar())
    return result


@tool
def transcribe_audio_clip(
    track: int,
    slot: int | None = None,
    arrangement_index: int | None = None,
    quantize: float = 0.25,
    start_beat: float | None = None,
    end_beat: float | None = None,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_ms: float = 80.0,
    min_freq_hz: float | None = 60.0,
    max_freq_hz: float | None = None,
    create_midi_clip: bool = False,
    follow_timing: bool = False,
) -> dict:
    """Transcribe an audio clip (e.g. a recorded guitar riff) to notes with basic-pitch.

    Uses the clip's loop region (or start/end markers) unless start_beat/end_beat (clip beats)
    are given, follows Live's warping, and applies the clip's transposition. Returned notes start
    at 0 = start of that region. quantize is the grid in beats (0 = off). Raise onset_threshold /
    frame_threshold (e.g. 0.6 / 0.4) if there are too many ghost notes; lower them if notes are
    missing. min_freq_hz=60 suits standard and drop tunings. create_midi_clip=true also writes
    the result to a new MIDI track next to the audio so the user can hear or edit it.
    follow_timing=true is for takes that drift off Live's grid: notes are returned on the
    performer's own grid (so quantizing reads cleanly), and a created MIDI clip is mapped back
    onto the performance so it plays in sync. Takes ~10-60 s; the first run is slower.
    """
    ref = _ref(track, slot, arrangement_index)
    ct = _clip_time(ref, start_beat, end_beat)
    clip, (start_beat, end_beat), region_s = ct.clip, ct.region, ct.region_s

    raw = transcribe_seconds(
        clip["file_path"], region_s[0], region_s[1],
        onset_threshold=onset_threshold, frame_threshold=frame_threshold,
        min_note_ms=min_note_ms, min_freq_hz=min_freq_hz, max_freq_hz=max_freq_hz,
    )
    length = ct.to_beats(region_s[1])
    shift = int(clip.get("pitch_coarse", 0))
    notes = []
    for n in raw:
        s, e = ct.to_beats(n["start_s"]), ct.to_beats(n["end_s"])
        s, e = max(0.0, s), min(length, e)
        pitch = n["pitch"] + shift
        if e > s and 0 <= pitch <= 127:
            notes.append({"pitch": pitch, "start": round(s, 4), "duration": round(e - s, 4),
                          "velocity": n["velocity"]})

    timing = None
    if follow_timing:
        bpb = _beats_per_bar()
        pulse = _pulse(ct, bpb)
        if pulse is None or pulse.confidence < audio_analysis.FOLLOW_MIN_CONFIDENCE:
            raise ValueError(f"timing too unclear to follow (confidence "
                             f"{pulse.confidence if pulse else 0}); run without follow_timing")
        o = ct.origin()
        for n in notes:
            s = pulse.to_nominal(o + n["start"]) - o
            e = pulse.to_nominal(o + n["start"] + n["duration"]) - o
            n["start"], n["duration"] = round(max(0.0, s), 4), round(max(0.01, e - s), 4)
        length = pulse.to_nominal(o + length) - o
        timing = {"performed_bpm": round(pulse.performed_bpm, 1), "pulse": pulse.tick_label,
                  "confidence": pulse.confidence,
                  "note": "note times are on the performer's grid; write parts with "
                          "follow_timing=<this clip id> to play them in sync"}
    notes = music.quantize(notes, quantize)

    result = {
        "source": {"name": clip["name"], "file_path": clip["file_path"],
                   "warped": clip["warping"], "region_beats": [start_beat, end_beat],
                   "clip_id": refs.clip_id(track, slot, arrangement_index)},
        "length_beats": round(length, 4),
        "notes": notes,
        "analysis": music.analyze(notes, _beats_per_bar()),
    }
    if timing:
        result["timing"] = timing
    if "start_time" in clip:
        result["source"]["arrangement_start_time"] = clip["start_time"]
    if create_midi_clip and notes:
        target = {"name": f"{clip['name'] or 'Riff'} (MIDI)", "length_beats": round(length, 4)}
        if "start_time" in clip:
            target["arrangement_start"] = clip["start_time"]
        else:
            target["slot"] = slot
        written = notes
        if follow_timing:
            o = ct.origin()
            written = [{**n, "start": round(max(0.0, pulse.to_actual(o + n["start"]) - o), 4),
                        "duration": round(max(0.01, pulse.to_actual(o + n["start"] + n["duration"])
                                              - pulse.to_actual(o + n["start"])), 4)}
                       for n in notes]
            target["length_beats"] = round(ct.length, 4)
        result["midi_clip"] = _write_part(notes=written, insert_after=track, **target)
    return result


@tool
def transcribe_audio_file(path: str, bpm: float, start_seconds: float = 0.0,
                          end_seconds: float | None = None, quantize: float = 0.25,
                          beats_per_bar: float = 4.0, onset_threshold: float = 0.5,
                          frame_threshold: float = 0.3, min_note_ms: float = 80.0,
                          min_freq_hz: float | None = 60.0) -> dict:
    """Transcribe an audio file on disk (wav/aiff/flac/mp3) that isn't in the Live set.
    Beat 0 = start_seconds; bpm converts seconds to beats."""
    raw = transcribe_seconds(path, start_seconds, end_seconds, onset_threshold=onset_threshold,
                             frame_threshold=frame_threshold, min_note_ms=min_note_ms,
                             min_freq_hz=min_freq_hz)
    k = bpm / 60
    notes = [{"pitch": n["pitch"], "start": round((n["start_s"] - start_seconds) * k, 4),
              "duration": round((n["end_s"] - n["start_s"]) * k, 4), "velocity": n["velocity"]}
             for n in raw]
    notes = music.quantize(notes, quantize)
    return {"notes": notes, "analysis": music.analyze(notes, beats_per_bar)}


@tool
def analyze_notes(notes: list[Note], beats_per_bar: float = 4.0, grid: float = 0.25) -> dict:
    """Key estimate, per-bar lowest note and pitch classes, and an onset rhythm profile."""
    return music.analyze([n.model_dump() for n in notes], beats_per_bar, grid)


# ------------------------------------------------------------------ writing


def _write_part(notes: list[dict], length_beats: float, name: str | None = None,
                track: int | None = None, slot: int | None = None,
                arrangement_start: float | None = None, instrument: str | None = None,
                overwrite: bool = False, insert_after: int | None = None) -> dict:
    out: dict = {}
    if track is None:
        index = -1 if insert_after is None else insert_after + 1
        track = live.call("create_midi_track", index=index, name=name)["index"]
        out["created_track"] = True
    out["track"] = track

    location = None
    if arrangement_start is not None:
        try:
            location = live.call("create_arrangement_clip", track=track,
                                 start_time=arrangement_start, length=length_beats, name=name)
        except AbletonError as e:
            out["arrangement_note"] = f"{e} - used Session slot 0 instead"
            slot = 0 if slot is None else slot
    if location is None:
        slot = 0 if slot is None else slot
        live.call("create_clip", track=track, slot=slot, length=length_beats, name=name,
                  overwrite=overwrite)
        location = {"track": track, "slot": slot}
    out["clip"] = location
    out["notes_added"] = live.call("add_notes", notes=notes, **location)["added"]

    if instrument:
        found = live.call("search_browser", query=instrument, max_results=5)["results"]
        if found:
            live.call("load_item", track=track, uri=found[0]["uri"])
            out["instrument"] = found[0]["path"]
        else:
            out["instrument"] = f"nothing matched '{instrument}' - try search_browser"
    return out


@tool
def write_midi_part(notes: list[Note], length_beats: float, name: str,
                    track: int | None = None, slot: int | None = None,
                    arrangement_start: float | None = None, instrument: str | None = None,
                    overwrite: bool = False, follow_timing: str | None = None) -> dict:
    """Write a generated part to Live in one step.

    Creates a new MIDI track called `name` unless `track` is given, then a clip of
    `length_beats` in Session `slot` (default 0) or at `arrangement_start` (beats) in the
    Arrangement, adds the notes, and optionally loads an instrument found by browser search
    (e.g. "kit" or "808" for drums, "bass", "Drift"). overwrite replaces an existing clip in the slot.
    follow_timing=<audio clip id, e.g. "t2/a0">: notes are written on the grid as if the
    recording were perfectly in time, then moved onto that recording's actual pulse.
    """
    data = [n.model_dump() for n in notes]
    info = None
    if follow_timing:
        data, info = _follow(data, follow_timing, arrangement_start)
    out = _write_part(data, length_beats, name, track, slot, arrangement_start, instrument, overwrite)
    if info:
        out["timing"] = info
    return out


@tool
def create_midi_track(name: str | None = None, index: int = -1) -> dict:
    """Create an empty MIDI track (index -1 = at the end)."""
    return live.call("create_midi_track", index=index, name=name)


@tool
def create_clip(track: int, slot: int, length_beats: float = 4.0, name: str | None = None,
                overwrite: bool = False) -> dict:
    """Create an empty MIDI clip in a Session View slot (adds scenes if needed)."""
    return live.call("create_clip", track=track, slot=slot, length=length_beats, name=name,
                     overwrite=overwrite)


@tool
def add_notes(track: int, notes: list[Note], slot: int | None = None,
              arrangement_index: int | None = None, replace: bool = False,
              follow_timing: str | None = None) -> dict:
    """Add notes to an existing MIDI clip. replace=true clears the clip first (one undo step).
    follow_timing=<audio clip id>: move grid-written notes onto that recording's pulse."""
    ref = _ref(track, slot, arrangement_index)
    data = [n.model_dump() for n in notes]
    info = None
    if follow_timing:
        target = live.call("get_clip", **ref)
        song_zero = (target["start_time"] - target["start_marker"]) if "start_time" in target else None
        data, info = _follow(data, follow_timing, song_zero)
    out = live.call("add_notes", notes=data, replace=replace, **ref)
    if info:
        out["timing"] = info
    return out


@tool
def clear_notes(track: int, slot: int | None = None, arrangement_index: int | None = None,
                from_beat: float = 0.0, to_beat: float | None = None,
                from_pitch: int = 0, to_pitch: int = 127) -> dict:
    """Remove notes from a MIDI clip, optionally only within a time and pitch range."""
    span = (to_beat - from_beat) if to_beat is not None else 1.0e6
    return live.call("remove_notes", from_time=from_beat, time_span=span,
                     from_pitch=from_pitch, pitch_span=to_pitch - from_pitch + 1,
                     **_ref(track, slot, arrangement_index))


@tool
def set_clip(track: int, slot: int | None = None, arrangement_index: int | None = None,
             name: str | None = None, looping: bool | None = None,
             loop_start: float | None = None, loop_end: float | None = None) -> dict:
    """Rename a clip or change its loop settings."""
    return live.call("set_clip", name=name, looping=looping, loop_start=loop_start,
                     loop_end=loop_end, **_ref(track, slot, arrangement_index))


@tool
def delete_clip(track: int, slot: int | None = None, arrangement_index: int | None = None) -> dict:
    """Delete a clip (the user can undo with Ctrl+Z)."""
    return live.call("delete_clip", **_ref(track, slot, arrangement_index))


@tool
def rename_track(track: int, name: str) -> dict:
    """Rename a track."""
    return live.call("set_track_name", track=track, name=name)


@tool
def set_tempo(bpm: float) -> dict:
    """Set the song tempo."""
    return live.call("set_tempo", bpm=bpm)


@tool
def set_time_signature(numerator: int, denominator: int) -> dict:
    """Set the song time signature."""
    return live.call("set_time_signature", numerator=numerator, denominator=denominator)


# ---------------------------------------------------------------- playback


@tool
def transport(action: Literal["play", "stop", "continue", "stop_all_clips"]) -> dict:
    """Start/stop the song or stop all clips."""
    return live.call("transport", action=action)


@tool
def fire_clip(track: int, slot: int) -> dict:
    """Launch a Session View clip slot."""
    return live.call("fire_clip", track=track, slot=slot)


@tool
def fire_scene(scene: int) -> dict:
    """Launch a whole scene (row) so the riff and the new parts play together."""
    return live.call("fire_scene", scene=scene)


@tool
def stop_track(track: int) -> dict:
    """Stop clips playing on one track."""
    return live.call("stop_track", track=track)


# ----------------------------------------------------------------- browser


@tool
def search_browser(query: str, categories: list[str] | None = None, max_results: int = 25) -> dict:
    """Search Live's browser for loadable items. Default categories: instruments, drums, sounds.
    Others: audio_effects, midi_effects, packs, user_library, samples, clips, plugins."""
    return live.call("search_browser", query=query, categories=categories,
                     max_results=max_results)


@tool
def browse(path: str = "") -> list:
    """List a browser folder, e.g. "" (roots), "drums", "instruments/Drift"."""
    return live.call("browse", path=path)


@tool
def load_instrument(track: int, uri: str | None = None, path: str | None = None) -> dict:
    """Load a browser item (from search_browser/browse) onto a track."""
    return live.call("load_item", track=track, uri=uri, path=path)


# ------------------------------------------------------------------ export


@tool
def write_midi_file(notes: list[Note], filename: str, bpm: float | None = None,
                    drums: bool = False, beats_per_bar: int = 4) -> dict:
    """Save notes as a .mid file in the project's exports folder (works without Live running),
    so it can be dragged into any DAW. drums=true writes on MIDI channel 10."""
    if bpm is None:
        try:
            bpm = live.call("get_session")["tempo"]
        except AbletonError:
            bpm = 120.0
    name = Path(filename).name
    if not name.lower().endswith(".mid"):
        name += ".mid"
    path = music.write_midi_file([n.model_dump() for n in notes], EXPORT_DIR / name, bpm,
                                 channel=9 if drums else 0, beats_per_bar=beats_per_bar)
    return {"path": str(path), "bpm": bpm, "notes": len(notes)}


# ------------------------------------------------------------ introspection


@tool
def get_project_state(detail: Literal["summary", "full"] = "summary") -> dict:
    """The whole Live Set as one structured view: tempo, time signature, key/scale, length,
    loop, locators, used scenes, and every track (incl. returns and master) with its ID, kind,
    guessed role (drums/bass/guitar/...), mute/solo/arm, volume/pan, active sends, device
    chain and clip spans in bars. detail="full" adds routing, device IDs and every clip ID."""
    return views.project(live.call("project_snapshot"), detail)


@tool
def get_track_state(track_id: str) -> dict:
    """One track in depth (track_id like "t3", "r0", "m"): mixer parameters with IDs, routing
    and monitoring, the full device tree (rack chains, Drum Rack pads with names), every clip
    with ID and bar span, and which parameters carry arrangement automation."""
    raw = live.call("track_state", track_id=track_id)
    return views.track_state(raw, _beats_per_bar())


@tool
def get_device_state(device_id: str, params: str = "changed") -> dict:
    """A device's parameters with display values (e.g. "-6.0 dB", "1.2 kHz"), automation
    state, rack chains / drum pads. device_id like "t3/d0", "t3/d0/c1/d0" (device in a rack
    chain), "t3/d0/n36/d0" (device on drum pad 36) or "t3/mx" (mixer).
    params: "changed" (non-default + automated + switches, default), "all", "automated",
    or a name filter such as "freq"."""
    return views.device_state(live.call("device_state", device_id=device_id), params)


@tool
def get_clip_state(clip_id: str, envelope_step_beats: float = 1.0) -> dict:
    """One clip's properties (clip_id like "t3/a0" or "t3/s1"): bars and position, loop and
    markers, launch settings, MIDI note summary or audio file/warp/gain/duration, plus clip
    automation envelopes sampled every envelope_step_beats (display values)."""
    raw = live.call("clip_state", clip_id=clip_id, envelope_step=envelope_step_beats)
    return views.clip_state(raw, _beats_per_bar())


# ----------------------------------------------------------------- analysis


@tool
def analyze_midi_clip(clip_id: str, start_bar: int | None = None, end_bar: int | None = None,
                      include_notes: bool = False) -> dict:
    """What is in a MIDI clip, without reading raw notes: density per bar, grid and feel
    (dominant subdivision, swing, deviation in ms), velocity/accent map, bar form letters,
    repeats, phrases, rhythm changes, pitch range + per-bar harmony, and for drum clips
    (Drum Rack pad names or GM) per-voice patterns, feel per section (backbeat / half-time /
    fills) and the timekeeping cymbal. Arrangement clips use song bars; Session clips use
    clip bars (bar 1 = clip start). Note: Live clips carry no MIDI channel."""
    raw = live.call("clip_notes", clip_id=clip_id)
    song = live.call("song_state")
    bpb = refs.beats_per_bar(song["signature"])
    pads = {int(k): v for k, v in (raw.get("drum_pads") or {}).items()}
    notes = midi_analysis.expand_clip_notes(raw)
    result = midi_analysis.analyze_clip(notes, bpb, song["tempo"], pads=pads or None,
                                        is_drums=bool(pads), start_bar=start_bar, end_bar=end_bar,
                                        include_notes=include_notes)
    head = {"clip": clip_id, "name": raw.get("name"), "track": raw.get("track_name"),
            "timeline": "song bars" if raw.get("start_time") is not None else "clip bars (1 = clip start)"}
    if raw.get("start_time") is not None:
        head["position"] = refs.pos(raw["start_time"], bpb)
    return {**head, **result}


@tool
def analyze_audio_clip(clip_id: str,
                       features: list[Literal["levels", "onsets", "timing", "spectrum"]] | None = None,
                       start_bar: int | None = None, end_bar: int | None = None) -> dict:
    """Analyze an audio clip's source file through Live's warp mapping, reported per bar:
    levels (peak/RMS dBFS, silent bars, clipping), onsets (attacks per bar), timing (performed
    tempo vs project, drift in ms per bar, pulse confidence - is the take in time?), spectrum
    (low/mid/high energy %, spectral centroid). Default: all features. Cached after the first
    run (~5-20 s). Live's API exposes no audio data, so this reads the clip's file directly."""
    wanted = set(features or ["levels", "onsets", "timing", "spectrum"])
    bpb = _beats_per_bar()
    ct = _clip_time(refs.parse_clip(clip_id))
    feats, bars = _audio_features(ct, bpb)
    onset_bars = [ct.bar(ct.to_beats(s), bpb) for s in feats["onsets_s"]]
    table = audio_analysis.per_bar(feats, bars, onset_bars)
    keep = [i for i, b in enumerate(table["bars"])
            if (start_bar is None or b >= start_bar) and (end_bar is None or b <= end_bar)]

    def col(name):
        return [table[name][i] for i in keep]

    out = {"clip": clip_id, "name": ct.clip.get("name"),
           "file": os.path.basename(ct.clip.get("file_path") or ""),
           "file_duration_s": feats["duration_s"], "warped": ct.clip.get("warping"),
           "timeline": "song bars" if ct.song_start is not None else "clip bars (1 = region start)"}
    per = {"bars": col("bars")}
    if "levels" in wanted:
        out["levels"] = {"file_peak_db": feats["peak_db"], "clipped_samples": feats["clipped_samples"],
                         "silent_bars": [r for r in table["silent_bars"]
                                         if (start_bar is None or r[1] >= start_bar)
                                         and (end_bar is None or r[0] <= end_bar)]}
        per["rms_db"], per["peak_db"] = col("rms_db"), col("peak_db")
    if "onsets" in wanted:
        counts = col("onsets")
        out["onsets"] = {"total": sum(counts),
                         "avg_per_bar": round(sum(counts) / max(1, len(counts)), 1)}
        per["onsets"] = counts
    if "spectrum" in wanted:
        cents = [c for c in col("centroid_hz") if c]
        out["spectrum"] = {"bands": "low <250 Hz, mid 250-2k, high >2k (% of energy)",
                           "median_centroid_hz": sorted(cents)[len(cents) // 2] if cents else None}
        per["low_pct"], per["mid_pct"], per["high_pct"] = col("low_pct"), col("mid_pct"), col("high_pct")
        per["centroid_hz"] = col("centroid_hz")
    if "timing" in wanted:
        onsets = [ct.origin() + ct.to_beats(s) for s in feats["onsets_s"]]
        pulse = audio_analysis.estimate_pulse(onsets, ct.clip["song_tempo"])
        out["timing"] = audio_analysis.timing_report(pulse, lambda beat: refs.bar_of(beat, bpb), bpb)
    out["per_bar"] = per
    return out


@tool
def get_arrangement_map(start_bar: int | None = None, end_bar: int | None = None,
                        analyze_audio: bool = False, tracks: list[str] | None = None) -> dict:
    """The song's structure in one call: sections (from locators, else inferred from
    activity/density/clip boundaries) labelled by similarity (A, B, A' ...), which tracks and
    roles play in each section, a per-bar activity strip per track, empty bars, and bars where
    a track's rhythm changes. Give start_bar/end_bar to ask about a range ("what plays in bars
    33-49?" -> in_range). analyze_audio=true also measures audio clips (silence, energy,
    onset density) instead of treating every audio clip as active; slower on first run."""
    snap = live.call("project_snapshot")
    song = snap["song"]
    bpb = refs.beats_per_bar(song["signature"])
    track_list = []
    for t in snap["tracks"]:
        if t["kind"] not in ("midi", "audio") or (tracks and t["id"] not in tracks):
            continue
        clips = [{"id": c["id"], "start": c["start"], "end": c["end"], "kind": c["kind"],
                  "muted": c.get("muted", False)} for c in t.get("arrangement_clips", [])]
        track_list.append({"id": t["id"], "name": t["name"], "kind": t["kind"],
                           "role": views.guess_role(t), "clips": clips})
    notes_by_track: dict[str, list[dict]] = {}
    midi_ids = [t["id"] for t in track_list if t["kind"] == "midi"]
    if midi_ids:
        for t in live.call("arrangement_notes", track_ids=midi_ids):
            notes_by_track[t["id"]] = [n for c in t["clips"] if not c.get("muted")
                                       for n in midi_analysis.expand_clip_notes(c)]
    audio_by_track: dict[str, dict] = {}
    if analyze_audio:
        for t in track_list:
            if t["kind"] != "audio":
                continue
            stats: dict[int, dict] = {}
            for c in t["clips"]:
                if c.get("muted"):
                    continue
                ct = _clip_time(refs.parse_clip(c["id"]))
                feats, bars = _audio_features(ct, bpb)
                counts: dict[int, int] = {}
                for s in feats["onsets_s"]:
                    b = ct.bar(ct.to_beats(s), bpb)
                    counts[b] = counts.get(b, 0) + 1
                for b, seg in zip(bars, feats["segments"]):
                    if seg is None:
                        continue
                    prev = stats.get(b)
                    stats[b] = {"rms_db": max(seg["rms_db"], prev["rms_db"]) if prev else seg["rms_db"],
                                "onsets": counts.get(b, 0) + (prev["onsets"] if prev else 0)}
            audio_by_track[t["id"]] = stats
    result = structure.arrangement_map(track_list, notes_by_track, audio_by_track,
                                       song.get("locators", []), bpb, start_bar, end_bar)
    head = {"tempo": song["tempo"], "time_signature": "%d/%d" % tuple(song["signature"]),
            "audio_analyzed": analyze_audio}
    if not analyze_audio and any(t["kind"] == "audio" for t in track_list):
        head["audio_note"] = ("audio tracks count as active wherever a clip exists; "
                              "analyze_audio=true detects silence and rhythm changes inside them")
    return {**head, **result}


@tool
def sample_meters(seconds: float = 2.0) -> dict:
    """Sample Live's output meters on every track, return and master for a few seconds
    (max 10) and report peak/average levels and which tracks are sounding. Values are Live's
    0-1 meter scale, not dBFS; only meaningful while Live is playing."""
    seconds = max(0.2, min(10.0, seconds))
    names = {t["id"]: t["name"] for t in live.call("project_snapshot", clips=False)["tracks"]}
    acc: dict[str, list[float]] = {}
    samples, playing = 0, False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        s = live.call("meter_sample")
        playing = playing or s["playing"]
        for tid, left, right in s["levels"]:
            acc.setdefault(tid, []).append(max(left, right))
        samples += 1
        time.sleep(0.05)
    rows = [{"id": tid, "name": names.get(tid, tid), "peak": round(max(v), 3),
             "avg": round(sum(v) / len(v), 3), "active": max(v) > 0.02}
            for tid, v in acc.items()]
    rows.sort(key=lambda r: -r["peak"])
    out = {"playing": playing, "samples": samples, "tracks": rows}
    if not playing:
        out["note"] = "Live was not playing - start playback (transport play) and sample again"
    return out


# ------------------------------------------------------------ change management


@tool
def edit_session(action: Literal["begin", "preview", "commit", "rollback"],
                 label: str | None = None) -> dict:
    """Group several edits so they can be reviewed and undone together.
    begin: start recording; every write tool afterwards stores an exact inverse.
    preview: list the changes plus per-clip note diffs (added/removed).
    commit: keep everything and close the session.  rollback: undo every recorded change,
    newest first. Audio-clip deletion and device replacement are flagged as not reversible.
    Independent of Live's Ctrl+Z history."""
    if action == "begin":
        return live.begin(label)
    if action == "preview":
        return live.preview()
    if action == "commit":
        return live.commit()
    return live.rollback()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
