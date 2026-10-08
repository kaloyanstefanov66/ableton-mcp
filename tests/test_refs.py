import pytest

from ableton_mcp import refs


def test_clip_ids_round_trip():
    assert refs.parse_clip("t3/a0") == {"track": 3, "arrangement_index": 0}
    assert refs.parse_clip(" t12/s4 ") == {"track": 12, "slot": 4}
    assert refs.clip_id(3, arrangement_index=0) == "t3/a0"
    assert refs.clip_id(12, slot=4) == "t12/s4"
    for bad in ("3/a0", "t3", "r0/a1", "t3/x1"):
        with pytest.raises(ValueError):
            refs.parse_clip(bad)


def test_bars_and_positions():
    bpb = refs.beats_per_bar([4, 4])
    assert bpb == 4.0
    assert refs.bar_of(0.0, bpb) == 1
    assert refs.bar_of(332.0, bpb) == 84
    assert refs.bar_of(7.99999999, bpb) == 3  # float noise lands in the right bar
    assert refs.bar_start(90, bpb) == 356.0
    assert refs.pos(357.25, bpb) == "90.2.2"
    assert refs.pos(0.0, bpb) == "1.1.1"
    assert refs.beats_per_bar([9, 8]) == 4.5
    assert refs.pos(4.5, 4.5) == "2.1.1"
