"""The Remote Script's version shims (Live 10 / 11 / 12) and its Python 2.7 / 3.7 syntax."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ClaudeMCP import compat, introspect as ix
from fake_live import Clip, Note

REMOTE = Path(__file__).resolve().parents[1] / "remote_script" / "ClaudeMCP"


class LegacyClip:
    """Live 10: tuple notes, no probability, time range first in remove_notes."""

    def __init__(self, notes=()):
        self.notes = list(notes)
        self.removed = []

    def get_notes(self, from_time, from_pitch, time_span, pitch_span):
        return tuple(self.notes)

    def set_notes(self, notes):
        self.notes.extend(notes)

    def remove_notes(self, from_time, from_pitch, time_span, pitch_span):
        self.removed.append((from_time, from_pitch, time_span, pitch_span))
        self.notes = []


class ExtendedClip(Clip):
    def __init__(self):
        Clip.__init__(self, "x")
        self.removed = []

    def add_new_notes(self, specs):
        self._notes.extend(specs)

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        self.removed.append((from_pitch, pitch_span, from_time, time_span))
        self._notes = []


def fake_live(extended=True):
    def spec(**kw):
        return SimpleNamespace(**kw)
    return SimpleNamespace(Clip=SimpleNamespace(MidiNoteSpecification=spec) if extended
                           else SimpleNamespace())


def test_legacy_note_api_round_trip():
    clip = LegacyClip([(36, 0.0, 0.25, 100, False)])
    assert compat.read_notes(clip) == [(36, 0.0, 0.25, 100.0, False, 1.0)]
    compat.add_notes(fake_live(extended=False), clip, [(38, 1.0, 0.25, 117.6, False, 0.5)])
    assert clip.notes[-1] == (38, 1.0, 0.25, 118, False)  # int velocity, probability dropped
    compat.remove_notes(clip, 0, 128, 0.0, 16.0)
    assert clip.removed == [(0.0, 0, 16.0, 128)]  # Live 10 order: time first


def test_extended_note_api_round_trip():
    clip = ExtendedClip()
    compat.add_notes(fake_live(), clip, [(36, 0.5, 0.25, 110.0, False, 0.8)])
    assert compat.read_notes(clip) == [(36, 0.5, 0.25, 110.0, False, 0.8)]
    compat.remove_notes(clip, 0, 128, 0.0, 16.0)
    assert clip.removed == [(0, 128, 0.0, 16.0)]
    compat.add_notes(fake_live(), clip, [])  # no-op, no error


def test_notes_compact_reads_both_apis():
    legacy = LegacyClip([(38, 1.0, 0.25, 90, True), (36, 0.0, 0.25, 100, False)])
    assert ix.notes_compact(legacy) == [[36, 0.0, 0.25, 100.0, 0], [38, 1.0, 0.25, 90.0, 1]]
    assert ix.notes_compact(Clip("c", notes=[Note(36, 0.0)])) == [[36, 0.0, 0.25, 100.0, 0]]


class Slot:
    def __init__(self, track, clip=None):
        self.track, self.clip, self.has_clip = track, clip, clip is not None

    def create_clip(self, length):
        self.clip, self.has_clip = Clip("", length=length), True

    def delete_clip(self):
        self.clip, self.has_clip = None, False


class Live11Track:
    """Live 11: no create_midi_clip, but duplicate_clip_to_arrangement exists."""

    def __init__(self, slots_used):
        self.clip_slots = [Slot(self, Clip("used") if used else None) for used in slots_used]
        self.arrangement_clips = []

    def duplicate_clip_to_arrangement(self, clip, start):
        self.arrangement_clips.append(Clip("dup", start=start, length=clip.length))


class Song:
    def __init__(self, track):
        self.track, self.scenes_created, self.scenes_deleted = track, 0, []

    def create_scene(self, index):
        self.scenes_created += 1
        self.track.clip_slots.append(Slot(self.track))

    def delete_scene(self, index):
        self.scenes_deleted.append(index)
        self.track.clip_slots.pop(index)


def test_arrangement_clip_via_session_uses_a_free_slot_and_cleans_up():
    track = Live11Track([True, False])
    song = Song(track)
    clip = compat.create_arrangement_midi_clip(song, track, 32.0, 8.0)
    assert clip.start_time == 32.0 and clip.length == 8.0
    assert [s.has_clip for s in track.clip_slots] == [True, False]  # temp clip removed
    assert song.scenes_created == 0


def test_arrangement_clip_via_session_adds_and_removes_a_scene_when_full():
    track = Live11Track([True])
    song = Song(track)
    compat.create_arrangement_midi_clip(song, track, 4.0, 4.0)
    assert song.scenes_created == 1 and song.scenes_deleted == [1]
    assert len(track.clip_slots) == 1


def test_arrangement_clip_native_and_unsupported():
    native = SimpleNamespace(create_midi_clip=lambda s, l: Clip("n", start=s, length=l))
    assert compat.create_arrangement_midi_clip(None, native, 8.0, 4.0).start_time == 8.0
    with pytest.raises(RuntimeError, match="Live 11"):
        compat.create_arrangement_midi_clip(None, SimpleNamespace(clip_slots=[]), 0.0, 4.0)
    assert compat.arrangement_clips(SimpleNamespace()) == []  # Live 10 has none


def test_features_per_version():
    class Clip12:
        get_notes_extended = None

    class Track12:
        arrangement_clips = create_midi_clip = None

    class Song12:
        begin_undo_step = scale_name = None

    live12 = SimpleNamespace(Clip=SimpleNamespace(Clip=Clip12), Track=SimpleNamespace(Track=Track12),
                             Song=SimpleNamespace(Song=Song12))
    f = compat.features(live12)
    assert f["note_api"] == "extended" and f["arrangement_write"] == "native" and f["undo_steps"]
    live10 = SimpleNamespace(Clip=SimpleNamespace(Clip=object), Track=SimpleNamespace(Track=object),
                             Song=SimpleNamespace(Song=object))
    f = compat.features(live10)
    assert f["note_api"] == "legacy" and f["arrangement_write"] is None
    assert not f["arrangement_clips"] and not f["undo_steps"]


PY3_ONLY = (ast.JoinedStr, ast.AnnAssign, ast.YieldFrom, ast.AsyncFunctionDef, ast.Await,
            ast.Nonlocal, ast.NamedExpr)


@pytest.mark.parametrize("path", sorted(REMOTE.glob("*.py")), ids=lambda p: p.name)
def test_remote_script_stays_python27_and_37_compatible(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 7))  # Live 11 runs Python 3.7
    assert "from __future__ import absolute_import, print_function, unicode_literals" in src
    for node in ast.walk(tree):
        assert not isinstance(node, PY3_ONLY), f"{path.name}:{node.lineno} uses Python-3-only syntax"
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            a = node.args
            assert not a.kwonlyargs and not a.posonlyargs, f"{path.name}: keyword/positional-only args"
            assert all(x.annotation is None for x in a.args), f"{path.name}: annotations"
        if isinstance(node, ast.FunctionDef):
            assert node.returns is None, f"{path.name}:{node.lineno}: return annotation"
