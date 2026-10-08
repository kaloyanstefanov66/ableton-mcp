from ableton_mcp import music
from ableton_mcp.midi_analysis import analyze_clip, drum_voice, expand_clip_notes, grid_feel


def n(pitch, start, dur=0.25, vel=100):
    return {"pitch": pitch, "start": start, "duration": dur, "velocity": vel}


def test_grid_detection_straight_triplet_swing():
    straight = [i * 0.5 for i in range(32)]
    assert grid_feel(straight, 120)["dominant"] == "1/8"
    triplets = [i / 3 for i in range(24)]
    assert grid_feel(triplets, 120)["dominant"] == "1/8T"
    swung = [b + off for b in range(8) for off in (0.0, 0.3, 0.5, 0.8)]  # 16ths at 60% swing
    g = grid_feel(swung, 120)
    assert g["humanized"] is True
    assert 58 <= g["swing_pct"]["1/16"] <= 62


def test_accents_density_and_velocity():
    notes = [n(60, b + s, vel=120 if s == 0 else 60) for b in range(8) for s in (0.0, 0.5)]
    a = analyze_clip(notes, 4.0, 120)
    assert a["velocity"]["accent_map"][:4] == "X.o."
    assert a["density"]["per_bar"] == [8, 8]
    assert a["velocity"]["dynamics"] == "wide"


def test_form_repeats_phrases_and_rhythm_changes():
    bar_a = [n(40, s) for s in (0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5)]
    bar_b = [n(43, s) for s in (0, 1, 2, 3)]
    notes = []
    for i, bar in enumerate([bar_a, bar_b, bar_a, bar_b, bar_a, bar_b]):
        notes += [{**x, "start": x["start"] + 4 * i} for x in bar]
    notes += [{**x, "start": x["start"] + 4 * 7} for x in bar_a]  # bar 7 empty, bar 8 = A
    a = analyze_clip(notes, 4.0, 120)
    assert a["form"]["bars"] == "ABABAB-A"
    assert a["form"]["rhythm_only"] == "ababab-a"
    assert {"bars": [1, 2], "pattern": "AB", "repeats_at": [3, 5]} in a["form"]["repeats"]
    assert a["form"]["phrases"][-1]["bars"] == [8, 8]  # entry after the empty bar
    assert a["form"]["rhythm_changes"][0] == {"bar": 2, "from": "a", "to": "b", "onsets": [8, 4]}
    assert a["harmony"]["bars"][0]["lowest"] == "E1"
    assert a["pitch"]["range"] == ["E1", "G1"]


def test_drum_mode_voices_and_feel():
    pads = {36: "Kick Plastic 90s Heavy Rock", 38: "Snare Stick Hit 2",
            42: "Hihat Closed Stick Hit", 48: "Crash China Stick Hit"}
    notes = []
    for bar in range(4):  # backbeat with hats
        o = bar * 4
        notes += [n(36, o), n(36, o + 2), n(38, o + 1), n(38, o + 3)]
        notes += [n(42, o + s * 0.5, vel=70) for s in range(8)]
    for bar in range(4, 8):  # half-time with china
        o = bar * 4
        notes += [n(36, o), n(38, o + 2)] + [n(48, o + s) for s in range(4)]
    d = analyze_clip(notes, 4.0, 120, pads=pads)["drums"]
    assert set(d["voices"]) == {"kick", "snare", "hihat", "china"}
    assert d["voices"]["snare"]["common_pattern"] == "....x.......x..."
    assert [(s["bars"], s["feel"], s["timekeeper"]) for s in d["sections"]] == [
        ([1, 4], "backbeat", "hihat"), ([5, 8], "half-time", "china")]


def test_drum_voice_names_and_gm_fallback():
    assert drum_voice("Crash China Stick Hit Cop Style", 48) == "china"
    assert drum_voice("Ride Bell Stick Heavy", 51) == "ride_bell"
    assert drum_voice("Hihat Foot West Coast Band", 44) == "hihat_pedal"
    assert drum_voice("Snare Sidestick", 37) == "sidestick"
    assert drum_voice(None, 46) == "hihat_open"
    assert drum_voice("Mystery", 36) == "kick"


def test_expand_arrangement_clip_notes():
    base = {"notes": [[36, 0.0, 0.25, 100, 0], [38, 1.0, 0.25, 100, 0], [40, 2.0, 0.25, 100, 1]],
            "start_marker": 0.0, "loop_start": 0.0, "loop_end": 2.0,
            "start_time": 16.0, "end_time": 22.0}
    looped = expand_clip_notes({**base, "looping": True})
    assert [(x["pitch"], x["start"]) for x in looped] == [(36, 16.0), (38, 17.0), (36, 18.0),
                                                          (38, 19.0), (36, 20.0), (38, 21.0)]
    once = expand_clip_notes({**base, "looping": False})
    assert [(x["pitch"], x["start"]) for x in once] == [(36, 16.0), (38, 17.0)]  # muted dropped
    session = expand_clip_notes({"notes": base["notes"]})
    assert [x["start"] for x in session] == [0.0, 1.0]


def test_legacy_music_analyze_unchanged():
    riff = [n(40, 0, 0.5), n(40, 0.5, 0.5), n(43, 1, 0.5), n(45, 1.5, 1), n(40, 2.5, 1.5), n(47, 2.5, 1.5)]
    a = music.analyze(riff)
    assert a["key_candidates"][0]["key"] in ("E minor", "E major")
    assert set(a) == {"note_count", "length_beats", "range", "key_candidates", "rhythm_profile", "bars"}
