# ableton-mcp

**Turn your recorded riffs into full backing tracks in Ableton Live, with Claude.**

Record a guitar (or bass, or keys) part. Claude listens to the take, works out its timing,
key and structure, and writes drums and bass straight into your set, locked to how you
actually played. You don't need a click: if your take speeds up or drifts, the new parts
follow it.

> "Write Pantera-meets-Deftones drums for my riff starting at bar 94."
> "Add a bass line that doubles the low notes of the guitar."
> "Make the drums less repetitive and build up into the first chug part."
> "Is my riff in time? Where does it speed up?"

It works with **Ableton Live 10, 11 and 12, in any edition**: Intro, Lite, Standard and Suite.
No Max for Live needed.

## How a backing track gets made

1. **You record** a riff into an audio track (or point Claude at an existing one).
2. **Claude reads the set**: the song map (sections, who plays where), plus your take's
   timing (tempo, drift, pulse), onsets and key. Basic Pitch transcribes the riff to MIDI.
3. **Claude writes the parts** into new MIDI tracks with a stock kit or bass:
   - drums: the kick locks to your chugs, with fills at phrase ends and builds into sections
   - bass: by default steady roots that anchor the riff; you can ask for more movement
   - anything else you ask for
4. **Timing-aware writing**: parts are written on a clean grid, then moved onto your take's
   real pulse (`follow_timing`), so they stay with you even when you rush or drag.
5. **You listen** inside an *edit session*. Keep it with commit, or undo all of it with
   rollback, and nothing else in your set is touched.

The optional `ableton-drums` skill gives Claude a drum vocabulary for many genres (rock, shoegaze,
alt/nu-metal, groove metal, thrash, metalcore, modern metal, post-hardcore) and for specific drummers
(Abe Cunningham, Vinnie Paul, Lars Ulrich, The Rev, Rob Bourdon, Colm Ó Cíosóig).

## Architecture

```
Claude ──MCP/stdio──> ableton_mcp.server (Python 3.10 venv)
          │  JSON lines on 127.0.0.1:9890        │  heavy audio work
          ▼                                      ▼
  ClaudeMCP Remote Script (inside Live)   worker process (librosa, basic-pitch)
  reads/writes the set on Live's thread   reads the clip's audio file, cached
```

The Remote Script only *extracts* facts and applies edits. The server *interprets* them
(statistics, sections, timing) and returns short, readable JSON, which keeps Live's main thread fast.

## Compatibility

| | Live 10 | Live 11 | Live 12 |
|---|---|---|---|
| Remote Script runs on | Python 2.7 | Python 3.7 | Python 3.11 |
| Read the set, mixer, devices, Session clips | ✓ | ✓ | ✓ |
| See and analyze Arrangement clips | – | ✓ | ✓ |
| Write parts into the Arrangement | – (uses a Session slot) | ✓ (via a temporary Session clip) | ✓ |
| Note probability | – | ✓ | ✓ |

Smaller differences (key/scale, which Live 12 reports only with Scale Mode on, and grouping a
write into one Ctrl+Z step) are detected at runtime and skipped where a version lacks them.

- **Editions:** everything works in Intro, Lite, Standard and Suite. Intro and Lite cap track
  and scene counts. When a create call hits a cap, Claude reports Live's error instead of retrying.
  Smaller editions also ship fewer instruments and kits; Claude searches the browser for what's
  installed.
- `get_project_state` reports `live_version` and `live_features`, so Claude adapts to the version
  it's talking to.
- Tested on Live 12. The Live 10 and 11 code paths go through `remote_script/ClaudeMCP/compat.py`.
  They are covered by unit tests with stubbed Live objects, plus a syntax check that keeps the
  script Python 2.7 and 3.7 compatible. Please open an issue if something misbehaves on your version.

## Requirements

- Ableton Live 10.1.13 or later (11 and 12 included), any edition. Older Live 10 builds work too,
  but the script has to be copied by hand (see step 2).
- [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.10 for the server. basic-pitch
  uses the lightweight ONNX/CoreML runtimes on 3.10 instead of TensorFlow.
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
   Live 10 builds older than 10.1.13 don't read the User Library: copy `remote_script/ClaudeMCP`
   into the `MIDI Remote Scripts` folder inside the Live application instead.
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
5. Recommended: install the drum-style skill for Claude Code, then start a new session:
   ```bash
   uv run python scripts/install_skill.py
   ```

After editing `remote_script/`, re-run step 2. Then switch the Control Surface to *None* and back
to *ClaudeMCP*: the script reloads itself, so Live doesn't need a restart.
Remote Script errors are logged to Live's `Log.txt` (in Live's Preferences folder).

Run the tests with `uv run pytest`.

## Tips for good results

- **Point at the take.** "My riff is on track 3 from bar 94" saves Claude guessing which audio is yours
  and which is a reference or backing track.
- **Clicks are optional.** Takes recorded without a click are fine. Claude measures the pulse and
  writes against it, so you don't need to warp the audio first.
- **Say what you want to hear.** Name a band, drummer or feel ("tight", "no half-time",
  "build with the cymbals before the riff"). Claude changes timekeepers and snare placement between
  sections rather than just making them louder.
- **Edit in sessions.** Every write happens inside `edit_session`: listen, then *commit* or
  *rollback*. Rollback doesn't depend on Live's own undo history.
- **Transcription limits.** Basic Pitch is approximate on distorted, dense guitar. Onsets, the lowest
  notes and per-bar pitch classes are reliable; expect some octave ghosts.

## Tools

### Understanding the set
| Tool | What it gives Claude |
|---|---|
| `get_project_state(detail)` | Tempo, key/scale, length, loop, locators, scenes, Live version and features, and every track/return/master with its **role** (drums/bass/guitar/…), mixer, devices and clip spans in bars |
| `get_arrangement_map(start_bar, end_bar, analyze_audio)` | **Sections** (from locators, else inferred) labelled A/B/A′, tracks and roles per section, a per-bar activity strip, empty bars, rhythm changes, and an `in_range` answer for bar ranges |
| `get_track_state(track_id)` | Mixer, routing, the full device tree (rack chains, Drum Rack pads by name), clips, automated parameters |
| `get_device_state(device_id, params)` | Parameters as display values ("-6.0 dB", "1.2 kHz"), filtered to what changed or is automated |
| `get_clip_state(clip_id)` | Position, loop/markers, note summary or audio file/warp details, **clip envelopes** as values |
| `sample_meters(seconds)` | Output meters per track while Live plays |

### Analyzing your take and existing parts
| Tool | What it gives Claude |
|---|---|
| `analyze_audio_clip(clip_id, features)` | Per bar: **timing** (performed BPM vs project, drift in ms, pulse, whether it's followable), **onsets**, **levels**, **spectrum** |
| `transcribe_audio_clip(...)` | The riff as notes (Basic Pitch). `follow_timing=true` returns it on the performer's own grid |
| `analyze_midi_clip(clip_id, start_bar, end_bar)` | Density, grid/feel/swing, accents, bar form letters, phrases, harmony. For drums: per-voice patterns, **feel per section** (backbeat / half-time / fills) and the timekeeping cymbal |

### Writing the backing track
| Tool | What it does |
|---|---|
| `write_midi_part(notes, length_beats, name, arrangement_start, instrument, follow_timing)` | New track + clip + notes + instrument in one step. With `follow_timing="t2/a0"` the notes are moved onto that take's pulse |
| `add_notes`, `clear_notes`, `create_midi_track`, `create_clip`, `set_clip`, `delete_clip`, `rename_track` | Finer-grained edits (`add_notes` also accepts `follow_timing`) |
| `search_browser`, `browse`, `load_instrument` | Find and load kits, basses and other instruments that are installed |
| `edit_session("begin" \| "preview" \| "commit" \| "rollback")` | Group edits, preview per-clip note diffs, keep or undo them all |
| `write_midi_file` | Export a part to `exports/*.mid` (works without Live) |

### Other
`set_tempo`, `set_time_signature`, `transport`, `fire_clip`, `fire_scene`, `stop_track`,
plus the legacy int-index readers `get_session`, `get_track`, `get_clip_notes`, `analyze_notes`
and `transcribe_audio_file`.

## IDs

Tools take and return string IDs, and names are always included next to them.

| ID | Meaning |
|---|---|
| `t3`, `r0`, `m` | track 3, return track A, master |
| `t3/s2`, `t3/a0` | Session clip in slot 2, first Arrangement clip (by start time) |
| `t3/d1`, `t3/d1/c0/d2`, `t3/d1/n36/d0` | device; device in rack chain 0; device on drum pad 36 |
| `t3/mx`, `t3/mx/vol`, `t3/d1/p5` | the track's mixer; a mixer parameter; a device parameter |

IDs are positional, so re-read state after adding or removing tracks, clips or devices.
Legacy tools take ints: `t3/a0` = `track=3, arrangement_index=0`. Bars are 1-based, and positions
use Live's `bar.beat.sixteenth`.

## Example: drums that follow a take recorded without a click

**"Is my riff in time?"** → `analyze_audio_clip("t2/a0", ["timing"], start_bar=84, end_bar=95)`:
```json
{"timing": {"performed_bpm": 128.9, "project_bpm": 120.0, "tempo_deviation_pct": 7.5,
            "pulse": "1/8", "confidence": 0.82, "followable": true,
            "summary": "played at ~128.9 BPM vs project 120 (+7.5%), pulse 1/8; ends 1441 ms ahead of Live's grid"}}
```
Kicks written on the grid with `follow_timing="t2/a0"` landed at beats 0.133, 0.605, 1.077 …, on the
guitar's actual attacks (0.14, 0.60, 1.12). They ended a beat ahead of the grid, just like the take.

**Checking the result** → `analyze_midi_clip("t3/a0")` → `drums.sections`:
```json
[{"bars": [84, 89], "feel": "backbeat", "timekeeper": "ride"},
 {"bars": [91, 94], "feel": "backbeat", "timekeeper": "crash"},
 {"bars": [96, 97], "feel": "half-time", "timekeeper": "china"},
 {"bars": [103, 104], "feel": "busy/fill", "timekeeper": "none"}]
```

## What Live's API can't do (and how this works around it)

| Limitation | Workaround |
|---|---|
| No access to audio sample data | Analyze the clip's source file in a worker, mapped through Live's warping |
| Live 11 can't create Arrangement clips directly | Create a Session clip, duplicate it to the Arrangement, remove the temporary clip |
| Live 10 can't see or create Arrangement clips from scripts | Parts go into Session slots; drag them into the Arrangement |
| Arrangement automation points aren't readable | Report *which* parameters are automated; clip envelopes are fully sampled |
| MIDI notes carry no channel | Documented; the channel only exists on track input routing |
| Arrangement time-signature changes aren't exposed | Bars use the song's current signature |
| Live's undo history is shared with your own edits | `edit_session` keeps its own inverse journal |
