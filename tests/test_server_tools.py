import json
import subprocess
import sys

from ClaudeMCP import introspect as ix
from fake_live import Clip, CuePoint, Device, Note, Pad, Chain, Song, Track

from ableton_mcp import server, views

# Every tool that existed before the introspection work, with its parameters at that time.
LEGACY = {
    "get_session": set(), "get_track": {"track"},
    "get_clip_notes": {"track", "slot", "arrangement_index", "analyze"},
    "transcribe_audio_clip": {"track", "slot", "arrangement_index", "quantize", "start_beat", "end_beat",
                              "onset_threshold", "frame_threshold", "min_note_ms", "min_freq_hz",
                              "max_freq_hz", "create_midi_clip"},
    "transcribe_audio_file": {"path", "bpm", "start_seconds", "end_seconds", "quantize", "beats_per_bar",
                              "onset_threshold", "frame_threshold", "min_note_ms", "min_freq_hz"},
    "analyze_notes": {"notes", "beats_per_bar", "grid"},
    "write_midi_part": {"notes", "length_beats", "name", "track", "slot", "arrangement_start",
                        "instrument", "overwrite"},
    "create_midi_track": {"name", "index"},
    "create_clip": {"track", "slot", "length_beats", "name", "overwrite"},
    "add_notes": {"track", "notes", "slot", "arrangement_index", "replace"},
    "clear_notes": {"track", "slot", "arrangement_index", "from_beat", "to_beat", "from_pitch", "to_pitch"},
    "set_clip": {"track", "slot", "arrangement_index", "name", "looping", "loop_start", "loop_end"},
    "delete_clip": {"track", "slot", "arrangement_index"}, "rename_track": {"track", "name"},
    "set_tempo": {"bpm"}, "set_time_signature": {"numerator", "denominator"},
    "transport": {"action"}, "fire_clip": {"track", "slot"}, "fire_scene": {"scene"},
    "stop_track": {"track"}, "search_browser": {"query", "categories", "max_results"},
    "browse": {"path"}, "load_instrument": {"track", "uri", "path"},
    "write_midi_file": {"notes", "filename", "bpm", "drums", "beats_per_bar"},
}
NEW = {"get_project_state", "get_track_state", "get_device_state", "get_clip_state",
       "analyze_midi_clip", "analyze_audio_clip", "get_arrangement_map", "sample_meters",
       "edit_session"}


def tools():
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


def test_legacy_tools_unchanged_and_new_tools_present():
    registered = tools()
    assert set(registered) == set(LEGACY) | NEW
    for name, params in LEGACY.items():
        schema = registered[name].parameters
        assert params <= set(schema["properties"]), name  # nothing removed
        added = set(schema["properties"]) - params
        assert not (added & set(schema.get("required", []))), f"{name} gained a required param"


def fake_snapshot():
    kit = Device("Acuff Kit", "Drum Rack",
                 pads=[Pad(36, "Kick", [Chain("Kick", [Device("Simpler", "Simpler")])])])
    drums = Track("Drums", "midi", devices=[kit],
                  arrangement=[Clip("Beat", start=332.0, length=88.0, notes=[Note(36, 0.0)])], sends=1)
    gtr = Track("3-Audio", "audio", devices=[Device("OMEGA Ampworks Granophyre", "VST3", dtype=2)],
                arrangement=[Clip("Audio", midi=False, start=332.0, length=74.0)], sends=1)
    song = Song([gtr, drums], returns=[Track("A-Reverb", "audio")], cues=[CuePoint("Chorus", 356.0)])
    return {"live_version": "12.2.7", "song": ix.song_view(song),
            "tracks": [ix.track_view(song, t, tid, kind) for tid, t, kind in ix.all_tracks(song)],
            "selected_track": "t1", "detail_clip": None}


def test_project_view_is_concise_and_semantic():
    p = views.project(fake_snapshot())
    assert p["song"]["key"] == "D Minor" and p["song"]["time_signature"] == "4/4"
    assert p["locators"] == [{"name": "Chorus", "bar": 90, "position": "90.1.1"}]
    gtr, drums = p["tracks"]
    assert (gtr["id"], gtr["role"], drums["role"]) == ("t0", "guitar", "drums")
    assert drums["clips"] == {"arrangement": 1, "bars": [84, 105]}
    assert drums["devices"] == ["Acuff Kit [Drum Rack]"]
    assert "sends" not in drums  # -inf sends omitted
    assert p["returns"][0]["kind"] == "return" and p["master"]["kind"] == "master"
    full = views.project(fake_snapshot(), "full")
    assert full["tracks"][1]["arrangement_clips"][0] == {
        "id": "t1/a0", "name": "Beat", "kind": "midi", "bars": [84, 105], "position": "84.1.1"}


def test_device_view_filters_parameters():
    raw = {"id": "t0/d0", "name": "EQ", "type": "audio_effect", "parameters": [
        {"id": "t0/d0/p0", "name": "Device On", "display": "On", "options": ["Off", "On"]},
        {"id": "t0/d0/p1", "name": "1 Frequency A", "display": "120 Hz", "changed": True, "value": 0.3},
        {"id": "t0/d0/p2", "name": "1 Gain A", "display": "0.0 dB", "changed": False},
        {"id": "t0/d0/p3", "name": "2 Frequency A", "display": "1 kHz", "automation": "automated"}]}
    changed = views.device_state(raw)
    assert [p["id"] for p in changed["parameters"]] == ["t0/d0/p0", "t0/d0/p1", "t0/d0/p3"]
    assert changed["parameters_shown"] == "3 of 4 (changed)"
    assert [p["id"] for p in views.device_state(raw, "freq")["parameters"]] == ["t0/d0/p1", "t0/d0/p3"]


def test_stdio_server_lists_tools_and_runs_pure_tool():
    p = subprocess.Popen([sys.executable, "-m", "ableton_mcp.server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def rpc(msg):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline()) if "id" in msg else None

    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert len(listed) == len(LEGACY) + len(NEW)
        res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "analyze_notes", "arguments": {"notes": [
                {"pitch": 40, "start": 0, "duration": 1}, {"pitch": 47, "start": 0, "duration": 1}]}}})
        assert not res["result"].get("isError")
        assert "key_candidates" in res["result"]["content"][0]["text"]
    finally:
        p.kill()
