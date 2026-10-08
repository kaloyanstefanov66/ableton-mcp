import copy

import pytest

from ableton_mcp.journal import JournaledConnection


class FakeLive:
    """Just enough of the Remote Script command set to exercise edit sessions."""

    def __init__(self):
        self.tempo = 120.0
        self.tracks = [{"name": "Gtr", "devices": []}]
        self.clips = {}  # (track, "s"/"a", index) -> clip dict
        self.calls = []

    def state(self):
        return copy.deepcopy((self.tempo, self.tracks, self.clips))

    def _key(self, a):
        return (a["track"], "s", a["slot"]) if a.get("slot") is not None else (a["track"], "a", a["arrangement_index"])

    def call(self, cmd, **a):
        a = {k: v for k, v in a.items() if v is not None}
        self.calls.append(cmd)
        if cmd == "song_state":
            return {"tempo": self.tempo, "signature": [4, 4]}
        if cmd == "set_tempo":
            self.tempo = a["bpm"]
            return {}
        if cmd == "create_midi_track":
            self.tracks.append({"name": a.get("name") or "MIDI", "devices": []})
            return {"index": len(self.tracks) - 1, "name": self.tracks[-1]["name"]}
        if cmd == "delete_track":
            assert self.tracks[a["index"]]["name"] == a["expect_name"]
            self.tracks.pop(a["index"])
            return {}
        if cmd == "get_track":
            return {"name": self.tracks[a["track"]]["name"],
                    "devices": [{"name": d} for d in self.tracks[a["track"]]["devices"]]}
        if cmd == "set_track_name":
            self.tracks[a["track"]]["name"] = a["name"]
            return {}
        if cmd == "create_clip":
            self.clips[(a["track"], "s", a["slot"])] = {"name": a.get("name"), "length": a["length"],
                                                        "looping": True, "loop_start": 0.0,
                                                        "loop_end": a["length"], "notes": []}
            return {}
        if cmd == "create_arrangement_clip":
            i = len([k for k in self.clips if k[0] == a["track"] and k[1] == "a"])
            self.clips[(a["track"], "a", i)] = {"name": a.get("name"), "start_time": a["start_time"],
                                                "end_time": a["start_time"] + a["length"],
                                                "looping": False, "loop_start": 0.0,
                                                "loop_end": a["length"], "notes": []}
            return {"track": a["track"], "arrangement_index": i}
        if cmd == "delete_clip":
            del self.clips[self._key(a)]
            return {}
        if cmd == "get_notes":
            if self._key(a) not in self.clips:
                raise ValueError("empty")
            return copy.deepcopy(self.clips[self._key(a)])
        if cmd == "get_clip":
            return copy.deepcopy(self.clips[self._key(a)])
        if cmd == "set_clip":
            c = self.clips[self._key(a)]
            for k in ("name", "looping", "loop_start", "loop_end"):
                if k in a:
                    c[k] = a[k]
            return {}
        if cmd == "add_notes":
            c = self.clips[self._key(a)]
            if a.get("replace"):
                c["notes"] = []
            c["notes"] += copy.deepcopy(a["notes"])
            return {"added": len(a["notes"])}
        if cmd == "remove_notes":
            self.clips[self._key(a)]["notes"] = []
            return {}
        if cmd == "load_item":
            self.tracks[a["track"]]["devices"].append("Acuff Kit")
            return {"loaded": "Acuff Kit"}
        if cmd == "delete_device":
            self.tracks[int(a["track_id"][1:])]["devices"].pop(a["index"])
            return {}
        raise AssertionError(f"unexpected command {cmd}")


NOTE = {"pitch": 36, "start": 0.0, "duration": 0.25, "velocity": 100}


def test_passthrough_outside_session():
    fake = FakeLive()
    live = JournaledConnection(fake)
    live.call("set_tempo", bpm=90)
    assert fake.tempo == 90 and live.journal.entries == []
    assert live.preview() == {"status": "no open session"}


def test_rollback_restores_everything_in_reverse():
    fake = FakeLive()
    fake.clips[(0, "s", 0)] = {"name": "Riff", "length": 4.0, "looping": True, "loop_start": 0.0,
                               "loop_end": 4.0, "notes": [dict(NOTE, pitch=40)]}
    before = fake.state()
    live = JournaledConnection(fake)
    live.begin("drums")
    t = live.call("create_midi_track", index=-1, name="Drums")["index"]
    loc = live.call("create_arrangement_clip", track=t, start_time=32.0, length=8.0, name="Beat")
    live.call("add_notes", track=t, arrangement_index=loc["arrangement_index"],
              notes=[dict(NOTE, start=float(i)) for i in range(3)])
    live.call("add_notes", track=0, slot=0, notes=[NOTE], replace=True)
    live.call("set_tempo", bpm=127)
    live.call("load_item", track=t, uri="x")

    p = live.preview()
    assert len(p["changes"]) == 6 and all(c["reversible"] for c in p["changes"])
    diffs = {d["clip"]: d for d in p["note_diffs"]}
    assert diffs["t0/s0"] == {"clip": "t0/s0", "before": 1, "after": 1, "added": 1, "removed": 1}
    assert diffs["t1/a0"]["added"] == 3

    r = live.rollback()
    assert r["undone"] == 6 and "failed" not in r
    assert fake.state() == before
    assert live.preview() == {"status": "no open session"}


def test_deleted_midi_clip_comes_back():
    fake = FakeLive()
    fake.clips[(0, "a", 0)] = {"name": "Riff", "start_time": 16.0, "end_time": 24.0, "looping": False,
                               "loop_start": 0.0, "loop_end": 8.0, "notes": [NOTE]}
    live = JournaledConnection(fake)
    live.begin(None)
    live.call("delete_clip", track=0, arrangement_index=0)
    assert (0, "a", 0) not in fake.clips
    live.rollback()
    restored = fake.clips[(0, "a", 0)]
    assert restored["start_time"] == 16.0 and restored["notes"] == [NOTE]


def test_commit_and_single_session():
    fake = FakeLive()
    live = JournaledConnection(fake)
    live.begin("x")
    with pytest.raises(ValueError):
        live.begin("y")
    live.call("set_track_name", track=0, name="Lead")
    assert live.commit() == {"status": "committed", "label": "x", "changes": 1}
    assert fake.tracks[0]["name"] == "Lead"
    assert live.rollback() == {"status": "no open session"}
