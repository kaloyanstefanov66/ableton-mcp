# ableton-mcp

Lets Claude **understand** an Ableton Live set (structure, mixer, devices, MIDI and audio
content, timing) and then write parts that fit it. Works with **Live 12 Lite**.

Ask things like:
- "What happens during the chorus?"
- "Which tracks play in bars 33–49?"
- "Where does the guitar change rhythm?"
- "Is my riff in time?"
- "Write Pantera-style drums that follow my guitar take."

```
Claude Code ──MCP/stdio──> ableton_mcp.server (Python 3.10 venv)
                │  JSON lines on 127.0.0.1:9890          │  heavy audio work
                ▼                                        ▼
  ClaudeMCP Remote Script (inside Live)        worker process (librosa, basic-pitch)
  extracts compact facts on Live's main thread  reads the clip's audio file, cached
```

The Remote Script only *extracts* facts. The server *interprets* them (statistics, segmentation,
timing) and returns concise, LLM-shaped JSON. That keeps Live's main thread fast.

## Requirements

- Ableton Live 11 or 12, any edition including Lite (no Max for Live needed)
- [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.10 for you. basic-pitch uses the
  lightweight ONNX/CoreML runtimes on 3.10 instead of TensorFlow.
- Windows or macOS

## Setup

1. Clone and install:
   ```bash
   git clone https://github.com/kaloyanstefanov66/ableton-mcp.git
   cd ableton-mcp
   uv sync
   ```
   On Windows inside the Claude desktop app, `AppData\Roaming` is sandboxed. If `uv sync` fails
   with a "Python minor version link" error, set `UV_PYTHON_INSTALL_DIR` to a folder outside it
   (e.g. `%USERPROFILE%\.uv\python`).
2. Install the Remote Script into Live's User Library (`Documents/Ableton` on Windows,
   `~/Music/Ableton` on macOS; pass a path to override):
   ```bash
   uv run python scripts/install_remote_script.py
   ```
3. Restart Live, then go to **Preferences → Link, Tempo & MIDI → Control Surface** and pick
   **ClaudeMCP** (Input/Output: None).
4. Register the server with your MCP client.

   **Claude Code:**
   ```bash
   claude mcp add --scope user ableton -- uv --directory /path/to/ableton-mcp run ableton-mcp
   ```
   **Claude Desktop** (`claude_desktop_config.json`):
   ```json
   {
     "mcpServers": {
       "ableton": {
         "command": "uv",
         "args": ["--directory", "/path/to/ableton-mcp", "run", "ableton-mcp"]
       }
     }
   }
   ```
5. Optional: install the drum-style skill (genres + drummers) for Claude Code, then start a new session:
   ```bash
   uv run python scripts/install_skill.py
   ```

After editing `remote_script/`, re-run step 2. Then switch the Control Surface to *None* and back
to *ClaudeMCP*: the script reloads itself, so Live doesn't need a restart.
Remote Script errors are logged to Live's `Log.txt` (in Live's Preferences folder).

Run the tests with `uv run pytest`.

## IDs

New tools take and return string IDs. Names are always included next to them.

| ID | Meaning |
|---|---|
| `t3`, `r0`, `m` | track 3, return track A, master |
| `t3/s2`, `t3/a0` | Session clip in slot 2, first Arrangement clip (by start time) |
| `t3/d1`, `t3/d1/c0/d2`, `t3/d1/n36/d0` | device; device in rack chain 0; device on drum pad 36 |
| `t3/mx` | the track's mixer as a pseudo device |
| `t3/d1/p5`, `t3/mx/vol`, `t3/mx/send0` | parameters |

IDs are positional, so re-read state after adding or removing tracks, clips or devices.
Legacy tools still take ints: `t3/a0` = `track=3, arrangement_index=0`.
Bars are 1-based; positions use Live's `bar.beat.sixteenth`.

## Tools

### Understanding the set (new)
| Tool | What it gives Claude | Why it isn't just "control" |
|---|---|---|
| `get_project_state(detail)` | Tempo, key/scale, length, loop, locators, used scenes, and every track/return/master with **role** (drums/bass/guitar/…), mute/solo/arm, volume/pan, active sends, device chain, clip spans in bars | One semantic snapshot instead of dozens of getters; roles are inferred |
| `get_track_state(track_id)` | Mixer params with IDs, routing/monitoring, full device tree (rack chains, Drum Rack pads by name), clips with bars, parameters carrying arrangement automation | Shows the signal chain and what is automated |
| `get_device_state(device_id, params)` | Parameters as display values ("-6.0 dB", "1.2 kHz"), changed-from-default, automation state, chains/pads; filter by `changed`/`all`/`automated`/name | Defaults to *what matters* (changed + automated + switches) |
| `get_clip_state(clip_id)` | Bars/position, loop/markers, launch settings, note summary or audio file/warp/gain/duration, **clip envelopes** sampled into display values | Automation read as values over time |

### Musical analysis (new)
| Tool | What it gives Claude |
|---|---|
| `analyze_midi_clip(clip_id, start_bar, end_bar)` | Density per bar, grid/feel (dominant subdivision, swing %, deviation in ms), accent map, bar **form letters**, repeats, phrases, rhythm changes, pitch range + per-bar harmony. For drum clips: per-voice patterns from the real pad names, **feel per bar range** (backbeat / half-time / fills) and timekeeping cymbal |
| `analyze_audio_clip(clip_id, features)` | From the clip's file through Live's warp map, per bar: **levels** (peak/RMS dBFS, silent bars, clipping), **onsets**, **timing** (performed BPM vs project, drift in ms per bar, pulse confidence), **spectrum** (low/mid/high %, centroid) |
| `get_arrangement_map(start_bar, end_bar, analyze_audio)` | **Sections** (from locators, else inferred) labelled A/B/A′ by similarity, roles and tracks per section, a per-bar activity strip per track, empty bars, rhythm changes per track, and an `in_range` answer for bar ranges |
| `sample_meters(seconds)` | Peak/avg output meters per track while Live plays: what is actually sounding right now |

### Safe editing (new)
`edit_session("begin" | "preview" | "commit" | "rollback")`: every write tool records an exact
inverse while a session is open. `preview` lists the changes plus per-clip note diffs, and
`rollback` undoes them newest-first. It doesn't depend on Live's Ctrl+Z history, which Live
merges unpredictably and which also contains the user's own edits.

### Writing with a human take
`write_midi_part(..., follow_timing="t2/a0")` and `add_notes(..., follow_timing=...)`: write
notes on the grid as if the recording were perfectly in time, and the server moves them onto the
take's detected pulse. `transcribe_audio_clip(..., follow_timing=true)` does the reverse: it
returns the riff on the performer's own grid, so it quantizes cleanly.

### Existing tools (unchanged)
| Area | Tools |
| --- | --- |
| Read (legacy, int indices) | `get_session`, `get_track`, `get_clip_notes`, `analyze_notes` |
| Audio → MIDI | `transcribe_audio_clip`, `transcribe_audio_file` |
| Write | `write_midi_part`, `create_midi_track`, `create_clip`, `add_notes`, `clear_notes`, `set_clip`, `delete_clip`, `rename_track` |
| Song | `set_tempo`, `set_time_signature`, `transport`, `fire_clip`, `fire_scene`, `stop_track` |
| Sounds | `search_browser`, `browse`, `load_instrument` |
| Export | `write_midi_file` → `exports/*.mid` (works without Live) |

## Examples

**"What happens during the chorus?" / "Which sections have no drums?"**
Claude calls `get_arrangement_map()`:
```json
{"form": "A B C A",
 "sections": [
   {"label": "A", "bars": [1, 8],  "roles": ["drums", "guitar"], "silent_tracks": ["t1"]},
   {"label": "B", "bars": [9, 16], "roles": ["bass", "drums", "guitar"],
    "active": [{"track": "t0", "name": "Drums", "notes_per_bar": 10.0, "active_pct": 100}, "..."]},
   {"label": "C", "bars": [17, 20], "roles": []},
   {"label": "A", "bars": [21, 24], "roles": ["drums", "guitar"]}],
 "activity": {"first_bar": 1, "tracks": {"t0 Drums": "################....####"}},
 "empty_bars": [[17, 20]],
 "rhythm_changes": {"t0": [{"bar": 9, "onsets_per_bar": [4, 8]}, {"bar": 21, "onsets_per_bar": [8, 4]}]}}
```
→ "The chorus (B, bars 9–16) is the only place the bass plays, and the drums double their
density. Bars 17–20 are empty: no drums or anything else."

**"Which tracks are active in bars 33–49?"** → `get_arrangement_map(start_bar=33, end_bar=49)`
returns `in_range.active` (with active bar runs per track) and `in_range.silent`.

**"Where does the guitar change rhythm?"** → `get_arrangement_map(analyze_audio=true)` gives
`rhythm_changes` for audio tracks (onset-density jumps). For a MIDI part,
`analyze_midi_clip("t4/a0")` gives `form.rhythm_changes` and a `rhythm_only` letter string like
`"aaaabbbbaaaa"`.

**"Is my riff in time?"** → `analyze_audio_clip("t2/a0", ["timing"])`:
```json
{"timing": {"performed_bpm": 127.6, "project_bpm": 120, "tempo_deviation_pct": 6.3,
            "pulse": "1/8", "confidence": 0.86, "followable": true,
            "summary": "played at ~127.6 BPM vs project 120 (+6.3%), pulse 1/8; ends 1150 ms ahead of Live's grid",
            "drift_ms": {"bars": [84, 85, "..."], "ms": [-40, -95, "..."]}}}
```

**"Write Pantera-style drums that follow my take"** (with the `ableton-drums` skill):
1. `get_arrangement_map` → sections.
2. `analyze_audio_clip(..., ["timing", "onsets"])` → the take drifts and is followable.
3. Read `references/genres/groove-metal.md` and `references/drummers/vinnie-paul.md`.
4. `get_track_state` → map voices to the kit's real pad names.
5. `edit_session("begin")` → `write_midi_part(..., follow_timing="t2/a0")`.
6. `analyze_midi_clip` → verify per-section feel → `edit_session("preview")` → the user listens → commit or rollback.

## What Live's API can't do (and how this works around it)

| Limitation | Workaround |
|---|---|
| No access to audio sample data | Analyze the clip's source file (`file_path`) in a worker, mapped through `beat_to_sample_time` warping |
| Arrangement (track) automation points aren't readable | Report *which* parameters are automated (`automation_state`); clip envelopes are fully sampled |
| MIDI notes carry no channel | Documented. The channel only exists on track input routing |
| Meters are live peak values, only while playing | `sample_meters` polls them during playback |
| Arrangement time-signature changes aren't exposed | Bars use the song's current signature |
| Live's undo history is shared with the user | `edit_session` keeps its own inverse journal |

## Notes on Lite
- Remote Scripts work in every Live edition, so nothing here needs Standard/Suite or Max for Live.
- Lite caps track and scene counts; create calls fail with Live's own error when you hit them.
- Lite lacks Live's built-in "Convert to MIDI"; basic-pitch covers that.
- Transcription of distorted or dense polyphonic guitar is approximate: onsets, the lowest
  notes and per-bar pitch classes are reliable; expect some octave ghosts.
