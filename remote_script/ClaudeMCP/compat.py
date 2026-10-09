"""Version shims so one Remote Script runs on Live 10 (Python 2.7), 11 (3.7) and 12 (3.11).

Pure functions over LOM objects. The `Live` module is passed in where needed, so tests can
use stubs. Keep this file Python 2.7 compatible: no f-strings, annotations or keyword-only args.

    Live 10   get_notes / set_notes / remove_notes (tuples, no probability), no arrangement_clips
    Live 11   *_notes_extended + MidiNoteSpecification, arrangement_clips,
              duplicate_clip_to_arrangement, begin_undo_step
    Live 12   Track.create_midi_clip (direct Arrangement clip creation), Scale Mode
"""
from __future__ import absolute_import, print_function, unicode_literals

import sys

ALL_TIME = 1.0e6


def _has(cls, attr):
    try:
        return cls is not None and hasattr(cls, attr)
    except Exception:
        return False


def features(Live):
    """What this Live version's API can do, for the server to report and adapt to."""
    clip_cls = getattr(getattr(Live, "Clip", None), "Clip", None)
    track_cls = getattr(getattr(Live, "Track", None), "Track", None)
    song_cls = getattr(getattr(Live, "Song", None), "Song", None)
    if _has(track_cls, "create_midi_clip"):
        arrangement_write = "native"
    elif _has(track_cls, "duplicate_clip_to_arrangement"):
        arrangement_write = "via_session"
    else:
        arrangement_write = None
    return {
        "python": "%d.%d" % sys.version_info[:2],
        "note_api": "extended" if _has(clip_cls, "get_notes_extended") else "legacy",
        "arrangement_clips": _has(track_cls, "arrangement_clips"),
        "arrangement_write": arrangement_write,
        "undo_steps": _has(song_cls, "begin_undo_step"),
        "scale_info": _has(song_cls, "scale_name"),
    }


# ----------------------------------------------------------------------- notes

def read_notes(clip):
    """[(pitch, start, duration, velocity, mute, probability)] for the whole clip."""
    if hasattr(clip, "get_notes_extended"):
        return [(int(n.pitch), float(n.start_time), float(n.duration), float(n.velocity),
                 bool(n.mute), float(getattr(n, "probability", 1.0)))
                for n in clip.get_notes_extended(0, 128, 0.0, ALL_TIME)]
    # Live 10: get_notes(from_time, from_pitch, time_span, pitch_span) -> (pitch, time, dur, vel, mute)
    return [(int(n[0]), float(n[1]), float(n[2]), float(n[3]), bool(n[4]), 1.0)
            for n in clip.get_notes(0.0, 0, ALL_TIME, 128)]


def add_notes(Live, clip, notes):
    """Add (pitch, start, duration, velocity, mute, probability) tuples to a MIDI clip."""
    if not notes:
        return
    spec = getattr(getattr(Live, "Clip", None), "MidiNoteSpecification", None)
    if spec is not None and hasattr(clip, "add_new_notes"):
        clip.add_new_notes(tuple(
            spec(pitch=p, start_time=s, duration=d, velocity=v, mute=m, probability=pr)
            for p, s, d, v, m, pr in notes))
    else:
        # Live 10 has no probability and takes integer velocities.
        clip.set_notes(tuple((p, s, d, int(round(v)), m) for p, s, d, v, m, _ in notes))


def remove_notes(clip, from_pitch=0, pitch_span=128, from_time=0.0, time_span=ALL_TIME):
    if hasattr(clip, "remove_notes_extended"):
        clip.remove_notes_extended(int(from_pitch), int(pitch_span), float(from_time),
                                   float(time_span))
    else:  # Live 10 takes the time range first
        clip.remove_notes(float(from_time), int(from_pitch), float(time_span), int(pitch_span))


# ----------------------------------------------------------------- arrangement

def arrangement_clips(track):
    try:
        return list(track.arrangement_clips)
    except Exception:  # Live 10 doesn't expose Arrangement clips
        return []


def find_arrangement_clip(track, start_time):
    best = None
    for i, c in enumerate(arrangement_clips(track)):
        d = abs(float(c.start_time) - float(start_time))
        if d < 1e-3 and (best is None or d < best[0]):
            best = (d, i, c)
    return (best[1], best[2]) if best else (None, None)


def create_arrangement_midi_clip(song, track, start_time, length):
    """Create an empty MIDI clip in the Arrangement and return it.

    Live 12 creates it directly. Live 11 can't, so an empty Session clip is created in a free
    slot, duplicated into the Arrangement and then removed again. Live 10 can't do either.
    """
    if hasattr(track, "create_midi_clip"):
        return track.create_midi_clip(float(start_time), float(length))
    if not hasattr(track, "duplicate_clip_to_arrangement"):
        raise RuntimeError("this Live version cannot create Arrangement clips from scripts "
                           "(needs Live 11 or later)")
    slots = list(track.clip_slots)
    index = next((i for i, s in enumerate(slots) if not s.has_clip), None)
    added_scene = index is None
    if added_scene:
        index = len(slots)
        song.create_scene(-1)
    slot = list(track.clip_slots)[index]
    slot.create_clip(float(length))
    try:
        track.duplicate_clip_to_arrangement(slot.clip, float(start_time))
    finally:
        slot.delete_clip()
        if added_scene:
            song.delete_scene(index)
    _, clip = find_arrangement_clip(track, start_time)
    if clip is None:
        raise RuntimeError("Live did not create the Arrangement clip at beat %s" % start_time)
    return clip


def delete_arrangement_clip(track, clip):
    if not hasattr(track, "delete_clip"):
        raise RuntimeError("this Live version cannot delete Arrangement clips from scripts; "
                           "delete it by hand")
    track.delete_clip(clip)
