---
name: ableton-drums
description: Write genre- or drummer-specific drum parts into the user's Ableton Live set through the `ableton` MCP server. Covers shoegaze, alt/nu-metal, rock, post-hardcore, thrash, groove metal, metalcore and modern/prog metal, plus drummer styles (Abe Cunningham/Deftones, Colm Ó Cíosóig/My Bloody Valentine, Lars Ulrich/Metallica, The Rev/Avenged Sevenfold, Vinnie Paul/Pantera, Rob Bourdon/Linkin Park). Use when the user asks for drums "in the style of", "like <band>", for a genre, or to make existing drums heavier, busier, half-time, more aggressive or more laid back.
---

# Ableton drums in a style

You write drums with the `ableton` MCP tools. This skill supplies the musical vocabulary
(references/) and the workflow that makes the result fit the user's actual song.

## Workflow

1. **Understand the song first.**
   - `get_project_state`: tempo, time signature, existing drum track (role `drums`), kit.
   - `get_arrangement_map`: sections and bars. With no locators, sections are inferred; confirm
     with the user which is verse/chorus/breakdown if it matters.
   - Reference part (riff):
     - MIDI → `analyze_midi_clip` (form, rhythm changes, accents, harmony).
     - Audio → `analyze_audio_clip(clip, ["timing", "onsets"])`. If `timing.followable` and
       `max_drift_ms` is beyond ~30 ms, the take drifts: plan to use `follow_timing`.
       For the riff's rhythm, `transcribe_audio_clip(..., follow_timing=true)` gives notes
       on the performer's own grid (clean bars even when the take is off Live's grid).
2. **Pick the style.** Read the matching file(s):
   - genres: `references/genres/{shoegaze, alt-metal, rock, post-hardcore, thrash, groove-metal, metalcore, modern-metal}.md`
   - drummers: `references/drummers/{abe-cunningham, colm-o-ciosoig, lars-ulrich, the-rev, vinnie-paul, rob-bourdon}.md`
   - A drummer file refines a genre file; read both when the user names a band.
   - Read `references/notation.md` once for the grid notation and velocity scale.
3. **Map pads, never assume GM.**
   - Existing kit: `get_track_state(track)` → Drum Rack `pads` (note + name).
   - No kit yet: `write_midi_part(..., instrument="<kit>")` with a kit from
     `references/kits.md`, then read its pads the same way.
   - Map each notation voice to a pad by name (china vs crash, ride vs ride bell, floor tom = lowest tom).
4. **Write inside an edit session.**
   - `edit_session("begin", label)`.
   - Build notes section by section from the grooves. Times are clip-relative beats; one 16th = 0.25.
   - Make every section's feel deliberate. Add fills only at phrase ends (see the style file).
   - Lock the kick to the riff where the style says so: use the riff's onsets/chugs from step 1.
   - `write_midi_part(notes, length_beats, name, arrangement_start=<section start beat>, follow_timing=<audio clip id if drifting>)`.
     Write one clip for the whole passage, or one per section if the user wants to edit sections separately.
5. **Verify, then hand over.**
   - `analyze_midi_clip` on what you wrote: `drums.sections` must show the intended feel per
     bar range (e.g. backbeat → half-time at the chorus) and the intended timekeeper (ride, china ...).
   - `edit_session("preview")`.
   - Tell the user what each section does and leave the session open so they can listen. Then
     `commit` on approval, or `rollback` to remove everything.

## Rules of taste
- Serve the riff: in heavy styles the kick doubles the guitar's chugs; in shoegaze/rock it supports, not mirrors.
- Contrast sections: change the timekeeper (hat → ride → crash/china) and the snare placement
  (backbeat ↔ half-time) between sections, not just volume.
- Humanize unless the style is deliberately machine-tight (Rob Bourdon, modern metal):
  - velocity ±4–8
  - timing ±5–10 ms (±0.01–0.02 beats at 120 BPM)
  - snare a hair late for laid-back styles
- Ghost notes are quiet (velocity 25–45) or they turn into clutter.
- Cymbal crashes mark arrivals: section starts, big chord changes, ends of fills.
- Odd meters: write the groove in the riff's grouping (e.g. 9/8 = 2+2+2+3) and keep backbeats on the group starts.
