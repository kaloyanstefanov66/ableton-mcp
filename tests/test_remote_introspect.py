import pytest

from ClaudeMCP import introspect as ix
from fake_live import (Chain, Clip, CuePoint, Device, Envelope, Note, Pad, Param, Song, Track)


def drum_track():
    kick = Pad(36, "Kick Plastic", [Chain("Kick", [Device("Simpler", "Simpler")])])
    snare = Pad(38, "Snare Stick Hit", [Chain("Snare", [Device("Simpler", "Simpler")])])
    empty = Pad(40, "Empty")
    rack = Device("Acuff Kit", "Drum Rack", pads=[kick, snare, empty],
                  params=[Param("Device On", 1.0, quantized=True, items=("Off", "On")),
                          Param("Macro 1", 0.0, mx=127.0, default=0.0)])
    clip = Clip("Beat", start=16.0, length=8.0, notes=[Note(36, 0.0), Note(38, 1.0)])
    return Track("Drums", "midi", devices=[rack], arrangement=[clip], sends=1)


def song():
    reverb = Track("A-Reverb", "audio")
    eq = Device("EQ Eight", "EQ Eight", dtype=2, params=[
        Param("Device On", 1.0, quantized=True, items=("Off", "On")),
        Param("1 Frequency A", 0.5, default=0.3, fmt=lambda v: f"{v * 2:.1f} kHz", automation_state=1),
        Param("1 Gain A", 0.0, default=0.0)])
    rack = Device("FX Rack", "Audio Effect Rack", dtype=2, chains=[Chain("Chain 1", [eq])],
                  params=[Param("Device On", 1.0)])
    gtr = Track("Guitar", "audio", devices=[rack],
                session=[Clip("Riff", midi=False, length=8.0,
                              envelopes={"Track Volume": Envelope(lambda t: 0.85 if t < 4 else 0.5)})])
    return Song([drum_track(), gtr], returns=[reverb], cues=[CuePoint("Chorus", 32.0), CuePoint("Intro", 0.0)])


def test_resolve_ids():
    s = song()
    assert ix.resolve_track(s, "t1").name == "Guitar"
    assert ix.resolve_track(s, "r0").name == "A-Reverb"
    assert ix.resolve_track(s, "m").name == "Master"
    assert ix.resolve_clip(s, "t0/a0").name == "Beat"
    assert ix.resolve_clip(s, "t1/s0").name == "Riff"
    with pytest.raises(ValueError):
        ix.resolve_clip(s, "t1/s1")  # empty slot
    with pytest.raises(IndexError):
        ix.resolve_track(s, "t9")
    kind, dev, _ = ix.resolve_device(s, "t1/d0/c0/d0")
    assert kind == "device" and dev.name == "EQ Eight"
    _, simpler, _ = ix.resolve_device(s, "t0/d0/n36/d0")
    assert simpler.name == "Simpler"
    assert ix.resolve_param(s, "t1/d0/c0/d0/p1").name == "1 Frequency A"
    assert ix.resolve_param(s, "t0/mx/vol").name == "Track Volume"
    assert ix.resolve_param(s, "t0/mx/send0").name == "Send 0"


def test_track_view_is_compact_and_named():
    s = song()
    v = ix.track_view(s, s.tracks[0], "t0", "midi")
    assert v["mixer"] == {"volume": "0.0 dB", "pan": "C", "sends": {"A-Reverb": "-inf dB"}}
    assert v["devices"][0] == {"id": "t0/d0", "name": "Acuff Kit", "class": "Drum Rack",
                               "type": "instrument", "rack": "drum_rack"}
    assert v["arrangement_clips"][0] == {"id": "t0/a0", "name": "Beat", "kind": "midi",
                                         "length": 8.0, "start": 16.0, "end": 24.0}
    assert v["routing"] == {"input": "All Ins", "output": "Master"}
    assert "mute" not in v and "solo" not in v  # defaults omitted


def test_device_tree_pads_and_chains():
    s = song()
    rack = ix.device_tree(s.tracks[0].devices[0], "t0/d0")
    assert [(p["note"], p["name"], p["id"]) for p in rack["pads"]] == [
        (36, "Kick Plastic", "t0/d0/n36"), (38, "Snare Stick Hit", "t0/d0/n38")]  # empty pad skipped
    fx = ix.device_tree(s.tracks[1].devices[0], "t1/d0")
    assert fx["chains"][0]["devices"][0]["id"] == "t1/d0/c0/d0"
    assert ix.first_drum_rack(s.tracks[0]) == {36: "Kick Plastic", 38: "Snare Stick Hit"}


def test_param_view_changed_options_and_automation():
    eq = song().tracks[1].devices[0].chains[0].devices[0]
    on, freq, gain = (ix.param_view(p, f"x/p{i}") for i, p in enumerate(eq.parameters))
    assert on["options"] == ["Off", "On"] and "changed" not in on
    assert freq["display"] == "1.0 kHz" and freq["changed"] is True and freq["automation"] == "automated"
    assert gain["changed"] is False


def test_automated_params_and_envelopes():
    s = song()
    found = ix.automated_params(s, s.tracks[1], "t1")
    assert found == []  # automation sits inside a rack chain, not top level
    clip = s.tracks[1].clip_slots[0].clip
    envs = ix.clip_envelopes(s, clip, s.tracks[1], "t1", step=1.0)
    assert envs == [{"param": "t1/mx/vol", "name": "Mixer: Volume",
                     "points": [[0.0, "0.0 dB"], [4.0, "-12.0 dB"]]}]


def test_song_view_and_missing_attributes():
    s = song()
    v = ix.song_view(s)
    assert [c["name"] for c in v["locators"]] == ["Intro", "Chorus"]  # sorted by time
    assert v["key"] == {"root": 2, "scale": "Minor"}
    s.scale_mode = False  # Live's default C Major with Scale Mode off is not a key
    assert "key" not in ix.song_view(s)
    del s.scale_mode
    del s.root_note  # older Live: no key info
    assert "key" not in ix.song_view(s)
    bare = Track("Bare", "audio", routing=False)
    assert ix.routing_view(bare) == {}
