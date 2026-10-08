from ableton_mcp.structure import arrangement_map

BPB = 4.0


def beat(pattern_bar, bars):
    """Repeat a list of (pitch, beat-in-bar) for the given 1-based bars."""
    return [{"pitch": p, "start": (b - 1) * BPB + s, "duration": 0.25, "velocity": 100}
            for b in bars for p, s in pattern_bar]


GROOVE = [(36, 0), (38, 1), (36, 2), (38, 3)]
BUSY = [(36, s * 0.5) for s in range(8)] + [(38, 1), (38, 3)]
BASS = [(28, 0), (28, 2)]


def song(locators=()):
    tracks = [
        {"id": "t0", "name": "Drums", "kind": "midi", "role": "drums",
         "clips": [{"start": 0.0, "end": 64.0}, {"start": 80.0, "end": 96.0}]},
        {"id": "t1", "name": "Bass", "kind": "midi", "role": "bass",
         "clips": [{"start": 32.0, "end": 64.0}]},
        {"id": "t2", "name": "Guitar", "kind": "audio", "role": "guitar",
         "clips": [{"start": 0.0, "end": 64.0}, {"start": 80.0, "end": 96.0}]},
    ]
    notes = {"t0": beat(GROOVE, range(1, 9)) + beat(BUSY, range(9, 17)) + beat(GROOVE, range(21, 25)),
             "t1": beat(BASS, range(9, 17))}
    return arrangement_map(tracks, notes, {}, list(locators), BPB)


def test_sections_labels_and_roles():
    m = song()
    spans = [(s["label"], s["bars"]) for s in m["sections"]]
    assert spans == [("A", [1, 8]), ("B", [9, 16]), ("C", [17, 20]), ("A", [21, 24])]
    assert m["form"] == "A B C A"
    by_label = {s["label"]: s for s in m["sections"]}
    assert by_label["B"]["roles"] == ["bass", "drums", "guitar"]
    assert by_label["C"]["roles"] == []  # the section with no drums (or anything)
    assert m["empty_bars"] == [[17, 20]]


def test_activity_strip_and_rhythm_changes():
    m = song()
    drums = m["activity"]["tracks"]["t0 Drums"]
    assert drums == "#" * 16 + "...." + "#" * 4
    changes = m["rhythm_changes"]["t0"]
    assert [c["bar"] for c in changes] == [9, 21]
    assert changes[0]["onsets_per_bar"] == [4, 8]


def test_range_query():
    m = arrangement_map(*song_args(), BPB, start_bar=9, end_bar=12)
    assert m["range_bars"] == [9, 12]
    assert {a["track"] for a in m["in_range"]["active"]} == {"t0", "t1", "t2"}
    m = arrangement_map(*song_args(), BPB, start_bar=1, end_bar=8)
    assert m["in_range"]["silent"] == ["t1"]


def song_args(locators=()):
    tracks = [
        {"id": "t0", "name": "Drums", "kind": "midi", "role": "drums",
         "clips": [{"start": 0.0, "end": 64.0}]},
        {"id": "t1", "name": "Bass", "kind": "midi", "role": "bass", "clips": [{"start": 32.0, "end": 64.0}]},
        {"id": "t2", "name": "Guitar", "kind": "audio", "role": "guitar", "clips": [{"start": 0.0, "end": 64.0}]},
    ]
    notes = {"t0": beat(GROOVE, range(1, 9)) + beat(BUSY, range(9, 17)), "t1": beat(BASS, range(9, 17))}
    return tracks, notes, {}, list(locators)


def test_locators_name_sections():
    m = song(locators=[{"name": "Verse", "time": 0.0}, {"name": "Chorus", "time": 32.0}])
    assert m["sections_source"] == "locators"
    assert [(s.get("name"), s["bars"]) for s in m["sections"]] == [("Verse", [1, 8]), ("Chorus", [9, 24])]


def test_audio_analysis_marks_silence_inside_clips():
    tracks, notes, _, locs = song_args()
    audio = {"t2": {b: {"rms_db": -20.0 if b <= 4 else -80.0, "onsets": 6 if b <= 4 else 0}
                    for b in range(1, 17)}}
    m = arrangement_map(tracks, notes, audio, locs, BPB)
    strip = m["activity"]["tracks"]["t2 Guitar"]
    assert strip.startswith("####~~~~")
