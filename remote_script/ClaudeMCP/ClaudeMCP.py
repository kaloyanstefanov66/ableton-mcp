"""Claude MCP bridge - an Ableton Live Remote Script (works in Live Lite).

Listens on 127.0.0.1:9890 for newline-delimited JSON requests:
    {"id": 1, "cmd": "get_session", "args": {}}
and answers with:
    {"id": 1, "ok": true, "result": ...}  or  {"id": 1, "ok": false, "error": "..."}

Socket I/O happens on background threads, but every Live API call is queued
and executed on Live's main thread inside update_display(), because the Live
API is not thread safe.
"""
from __future__ import absolute_import, print_function, unicode_literals

import json
import queue
import socket
import threading
import traceback

import Live
from _Framework.ControlSurface import ControlSurface

from . import introspect as ix

HOST = "127.0.0.1"
PORT = 9890
REQUEST_TIMEOUT = 30.0
MAX_REQUESTS_PER_TICK = 25
BROWSER_ROOTS = (
    "instruments", "drums", "sounds", "audio_effects", "midi_effects",
    "packs", "user_library", "current_project", "samples", "clips", "plugins",
)
SEARCH_VISIT_LIMIT = 6000


class ClaudeMCP(ControlSurface):

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._requests = queue.Queue()
        self._running = True
        self._server = None
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._browser_cache = {}
        self._commands = {
            name[4:]: getattr(self, name) for name in dir(self) if name.startswith("cmd_")
        }
        try:
            self._start_server()
            self.log_message("ClaudeMCP: listening on %s:%d" % (HOST, PORT))
            self.show_message("Claude MCP: ready on port %d" % PORT)
        except Exception as e:
            self.log_message("ClaudeMCP: could not start server: %s" % e)
            self.show_message("Claude MCP: could not open port %d (%s)" % (PORT, e))

    # ------------------------------------------------------------------ server

    def _start_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind((HOST, PORT))
        srv.listen(4)
        srv.settimeout(1.0)
        self._server = srv
        t = threading.Thread(target=self._accept_loop, name="ClaudeMCP-accept")
        t.daemon = True
        t.start()

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self._clients.add(conn)
            t = threading.Thread(target=self._client_loop, args=(conn,), name="ClaudeMCP-client")
            t.daemon = True
            t.start()

    def _client_loop(self, conn):
        buf = b""
        try:
            while self._running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        response = self._submit(line)
                        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _submit(self, line):
        """Called on a socket thread: hand the request to the main thread and wait."""
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError as e:
            return {"id": None, "ok": False, "error": "invalid JSON: %s" % e}
        item = {"req": req, "done": threading.Event(), "resp": None}
        self._requests.put(item)
        if not item["done"].wait(REQUEST_TIMEOUT):
            return {"id": req.get("id"), "ok": False,
                    "error": "timed out waiting for Live's main thread"}
        return item["resp"]

    def update_display(self):
        ControlSurface.update_display(self)
        for _ in range(MAX_REQUESTS_PER_TICK):
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                break
            item["resp"] = self._execute(item["req"])
            item["done"].set()

    def _execute(self, req):
        rid = req.get("id")
        cmd = req.get("cmd")
        handler = self._commands.get(cmd)
        if handler is None:
            return {"id": rid, "ok": False, "error": "unknown command: %s" % cmd}
        try:
            return {"id": rid, "ok": True, "result": handler(**(req.get("args") or {}))}
        except Exception as e:
            self.log_message("ClaudeMCP: %s failed\n%s" % (cmd, traceback.format_exc()))
            return {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def disconnect(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._clients_lock:
            for conn in list(self._clients):
                try:
                    conn.close()
                except OSError:
                    pass
            self._clients.clear()
        while True:
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                break
            item["resp"] = {"id": item["req"].get("id"), "ok": False, "error": "script unloaded"}
            item["done"].set()
        ControlSurface.disconnect(self)

    # ----------------------------------------------------------------- helpers

    def _app(self):
        return Live.Application.get_application()

    def _track(self, index):
        tracks = self.song().tracks
        if not isinstance(index, int) or not 0 <= index < len(tracks):
            raise IndexError("track %s does not exist (the set has %d tracks, indices 0-%d)"
                             % (index, len(tracks), len(tracks) - 1))
        return tracks[index]

    def _clip_slot(self, track, slot):
        t = self._track(track)
        slots = t.clip_slots
        if not isinstance(slot, int) or not 0 <= slot < len(slots):
            raise IndexError("slot %s does not exist on track %d (it has %d slots)"
                             % (slot, track, len(slots)))
        return slots[slot]

    def _clip(self, track, slot=None, arrangement_index=None):
        if arrangement_index is not None:
            clips = list(self._track(track).arrangement_clips)
            if not 0 <= arrangement_index < len(clips):
                raise IndexError("track %d has %d arrangement clips" % (track, len(clips)))
            return clips[arrangement_index]
        if slot is None:
            raise ValueError("pass either slot (Session View) or arrangement_index (Arrangement View)")
        cs = self._clip_slot(track, slot)
        if not cs.has_clip:
            raise ValueError("track %d slot %d is empty" % (track, slot))
        return cs.clip

    def _track_type(self, t):
        if t.is_foldable:
            return "group"
        if t.has_midi_input:
            return "midi"
        if t.has_audio_input:
            return "audio"
        return "unknown"

    def _clip_summary(self, clip):
        d = {
            "name": clip.name,
            "is_midi": clip.is_midi_clip,
            "length": clip.length,
            "looping": clip.looping,
            "loop_start": clip.loop_start,
            "loop_end": clip.loop_end,
            "start_marker": clip.start_marker,
            "end_marker": clip.end_marker,
            "is_playing": clip.is_playing,
        }
        if getattr(clip, "is_arrangement_clip", False):
            d["start_time"] = clip.start_time
            d["end_time"] = clip.end_time
        if clip.is_audio_clip:
            d["warping"] = clip.warping
        return d

    def _audio_details(self, clip):
        d = {
            "file_path": clip.file_path,
            "warping": clip.warping,
            "warp_mode": int(clip.warp_mode),
            "pitch_coarse": clip.pitch_coarse,
            "pitch_fine": clip.pitch_fine,
            "gain": clip.gain,
        }
        for attr in ("sample_rate", "sample_length"):
            if hasattr(clip, attr):
                d[attr] = getattr(clip, attr)
        markers = getattr(clip, "warp_markers", None)
        if markers is not None:
            d["warp_markers"] = [{"beat_time": m.beat_time, "sample_time": m.sample_time}
                                 for m in markers]
        return d

    def _locate_clip(self, clip):
        """Find (track index, slot or arrangement index) of a clip object."""
        if clip is None:
            return None
        for ti, t in enumerate(self.song().tracks):
            for si, cs in enumerate(t.clip_slots):
                if cs.has_clip and cs.clip == clip:
                    return {"track": ti, "slot": si}
            try:
                for ai, c in enumerate(t.arrangement_clips):
                    if c == clip:
                        return {"track": ti, "arrangement_index": ai}
            except Exception:
                pass
        return None

    def _undo_step(self):
        song = self.song()
        return _UndoStep(song)

    # ---------------------------------------------------------------- commands

    def cmd_ping(self):
        app = self._app()
        return {
            "pong": True,
            "live_version": "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(),
                                          app.get_bugfix_version()),
        }

    def cmd_get_session(self):
        song = self.song()
        view = song.view
        tracks = []
        selected_track = None
        for i, t in enumerate(song.tracks):
            if t == view.selected_track:
                selected_track = i
            clips = []
            for si, cs in enumerate(t.clip_slots):
                if cs.has_clip:
                    c = cs.clip
                    clips.append({"slot": si, "name": c.name, "is_midi": c.is_midi_clip,
                                  "length": c.length})
            try:
                arr = [{"arrangement_index": ai, "name": c.name, "is_midi": c.is_midi_clip,
                        "start_time": c.start_time, "end_time": c.end_time}
                       for ai, c in enumerate(t.arrangement_clips)]
            except Exception:
                arr = []
            tracks.append({
                "index": i,
                "name": t.name,
                "type": self._track_type(t),
                "mute": t.mute,
                "solo": t.solo,
                "devices": [d.name for d in t.devices],
                "session_clips": clips,
                "arrangement_clips": arr,
            })
        highlighted = None
        hcs = view.highlighted_clip_slot
        if hcs is not None:
            for ti, t in enumerate(song.tracks):
                for si, cs in enumerate(t.clip_slots):
                    if cs == hcs:
                        highlighted = {"track": ti, "slot": si, "has_clip": cs.has_clip}
        info = self.cmd_ping()
        info.update({
            "tempo": song.tempo,
            "time_signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
            "is_playing": song.is_playing,
            "scene_count": len(song.scenes),
            "selected_track": selected_track,
            "highlighted_clip_slot": highlighted,
            "detail_clip": self._locate_clip(view.detail_clip),
            "tracks": tracks,
            "return_tracks": [t.name for t in song.return_tracks],
        })
        return info

    def cmd_get_track(self, track):
        t = self._track(track)
        session = []
        for si, cs in enumerate(t.clip_slots):
            if cs.has_clip:
                d = self._clip_summary(cs.clip)
                d["slot"] = si
                session.append(d)
        arrangement = []
        try:
            for ai, c in enumerate(t.arrangement_clips):
                d = self._clip_summary(c)
                d["arrangement_index"] = ai
                arrangement.append(d)
        except Exception:
            pass
        return {
            "index": track,
            "name": t.name,
            "type": self._track_type(t),
            "devices": [{"name": d.name, "class_name": d.class_name} for d in t.devices],
            "slot_count": len(t.clip_slots),
            "session_clips": session,
            "arrangement_clips": arrangement,
        }

    def cmd_get_clip(self, track, slot=None, arrangement_index=None):
        clip = self._clip(track, slot, arrangement_index)
        d = self._clip_summary(clip)
        if clip.is_audio_clip:
            d.update(self._audio_details(clip))
        d["song_tempo"] = self.song().tempo
        return d

    def cmd_get_notes(self, track, slot=None, arrangement_index=None):
        clip = self._clip(track, slot, arrangement_index)
        if not clip.is_midi_clip:
            raise ValueError("that is an audio clip - transcribe it instead")
        notes = clip.get_notes_extended(0, 128, 0.0, 1.0e6)
        out = [{
            "pitch": n.pitch,
            "start": n.start_time,
            "duration": n.duration,
            "velocity": n.velocity,
            "mute": n.mute,
            "probability": n.probability,
        } for n in notes]
        out.sort(key=lambda n: (n["start"], n["pitch"]))
        d = self._clip_summary(clip)
        d["notes"] = out
        return d

    def cmd_audio_beat_to_seconds(self, beats, track, slot=None, arrangement_index=None):
        """Map clip-local beat times to positions in the source audio file (warped clips)."""
        clip = self._clip(track, slot, arrangement_index)
        if not clip.is_audio_clip:
            raise ValueError("not an audio clip")
        if not clip.warping:
            raise ValueError("clip is not warped")
        return [clip.beat_to_sample_time(float(b)) for b in beats]

    def cmd_create_midi_track(self, index=-1, name=None):
        song = self.song()
        song.create_midi_track(index)
        new_index = len(song.tracks) - 1 if index < 0 else index
        t = song.tracks[new_index]
        if name:
            t.name = name
        return {"index": new_index, "name": t.name}

    def cmd_create_audio_track(self, index=-1, name=None):
        song = self.song()
        song.create_audio_track(index)
        new_index = len(song.tracks) - 1 if index < 0 else index
        t = song.tracks[new_index]
        if name:
            t.name = name
        return {"index": new_index, "name": t.name}

    def cmd_set_track_name(self, track, name):
        self._track(track).name = name
        return {"index": track, "name": name}

    def cmd_create_clip(self, track, slot, length=4.0, name=None, overwrite=False):
        t = self._track(track)
        if not t.has_midi_input:
            raise ValueError("track %d (%s) is not a MIDI track" % (track, t.name))
        song = self.song()
        while slot >= len(t.clip_slots):
            song.create_scene(-1)
        cs = self._clip_slot(track, slot)
        if cs.has_clip:
            if not overwrite:
                raise ValueError("track %d slot %d already has a clip (pass overwrite=true)"
                                 % (track, slot))
            cs.delete_clip()
        cs.create_clip(float(length))
        if name:
            cs.clip.name = name
        return {"track": track, "slot": slot, "length": cs.clip.length}

    def cmd_create_arrangement_clip(self, track, start_time, length, name=None):
        t = self._track(track)
        if not hasattr(t, "create_midi_clip"):
            raise RuntimeError("this Live version cannot create arrangement clips from scripts; "
                               "use a Session View slot instead")
        clip = t.create_midi_clip(float(start_time), float(length))
        if name:
            clip.name = name
        return self._locate_clip(clip)

    def cmd_delete_clip(self, track, slot=None, arrangement_index=None, expect_start=None):
        if arrangement_index is not None:
            clip = self._clip(track, None, arrangement_index)
            if expect_start is not None and abs(clip.start_time - float(expect_start)) > 1e-3:
                raise ValueError("arrangement clip %d on track %d starts at %s, expected %s"
                                 % (arrangement_index, track, clip.start_time, expect_start))
            self._track(track).delete_clip(clip)
        else:
            cs = self._clip_slot(track, slot)
            if cs.has_clip:
                cs.delete_clip()
        return {"deleted": True}

    def cmd_set_clip(self, track, slot=None, arrangement_index=None, name=None,
                     looping=None, loop_start=None, loop_end=None):
        clip = self._clip(track, slot, arrangement_index)
        if name is not None:
            clip.name = name
        if looping is not None:
            clip.looping = bool(looping)
        # Order matters: Live rejects loop_start >= loop_end at every step.
        if loop_end is not None and loop_end > clip.loop_end:
            clip.loop_end = float(loop_end)
        if loop_start is not None:
            clip.loop_start = float(loop_start)
        if loop_end is not None:
            clip.loop_end = float(loop_end)
        return self._clip_summary(clip)

    def cmd_add_notes(self, track, notes, slot=None, arrangement_index=None, replace=False):
        clip = self._clip(track, slot, arrangement_index)
        if not clip.is_midi_clip:
            raise ValueError("target clip is not a MIDI clip")
        specs = []
        for i, n in enumerate(notes):
            pitch = int(n["pitch"])
            start = float(n["start"])
            duration = float(n["duration"])
            velocity = float(n.get("velocity", 100))
            if not 0 <= pitch <= 127:
                raise ValueError("note %d: pitch %d out of range 0-127" % (i, pitch))
            if duration <= 0:
                raise ValueError("note %d: duration must be > 0" % i)
            if start < 0:
                raise ValueError("note %d: start must be >= 0" % i)
            specs.append(Live.Clip.MidiNoteSpecification(
                pitch=pitch,
                start_time=start,
                duration=duration,
                velocity=max(1.0, min(127.0, velocity)),
                mute=bool(n.get("mute", False)),
                probability=float(n.get("probability", 1.0)),
            ))
        with self._undo_step():
            if replace:
                clip.remove_notes_extended(0, 128, 0.0, 1.0e6)
            if specs:
                clip.add_new_notes(tuple(specs))
        return {"added": len(specs), "replaced": bool(replace)}

    def cmd_remove_notes(self, track, slot=None, arrangement_index=None,
                         from_time=0.0, time_span=1.0e6, from_pitch=0, pitch_span=128):
        clip = self._clip(track, slot, arrangement_index)
        clip.remove_notes_extended(int(from_pitch), int(pitch_span),
                                   float(from_time), float(time_span))
        return {"removed": True}

    def cmd_set_tempo(self, bpm):
        self.song().tempo = float(bpm)
        return {"tempo": self.song().tempo}

    def cmd_set_time_signature(self, numerator, denominator):
        song = self.song()
        song.signature_numerator = int(numerator)
        song.signature_denominator = int(denominator)
        return {"time_signature": "%d/%d" % (song.signature_numerator, song.signature_denominator)}

    def cmd_transport(self, action):
        song = self.song()
        if action == "play":
            song.start_playing()
        elif action == "stop":
            song.stop_playing()
        elif action == "continue":
            song.continue_playing()
        elif action == "stop_all_clips":
            song.stop_all_clips()
        else:
            raise ValueError("action must be play, stop, continue or stop_all_clips")
        return {"is_playing": song.is_playing}

    def cmd_fire_clip(self, track, slot):
        self._clip_slot(track, slot).fire()
        return {"fired": True}

    def cmd_stop_track(self, track):
        self._track(track).stop_all_clips()
        return {"stopped": True}

    def cmd_fire_scene(self, scene):
        scenes = self.song().scenes
        if not 0 <= scene < len(scenes):
            raise IndexError("scene %d does not exist (%d scenes)" % (scene, len(scenes)))
        scenes[scene].fire()
        return {"fired": True}

    # ----------------------------------------------------------------- browser

    def _item_info(self, item, path):
        self._browser_cache[item.uri] = item
        return {"name": item.name, "path": path, "uri": item.uri,
                "is_folder": item.is_folder, "is_loadable": item.is_loadable}

    def _resolve_path(self, path):
        browser = self._app().browser
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts or parts[0] not in BROWSER_ROOTS or not hasattr(browser, parts[0]):
            raise ValueError("path must start with one of: %s" % ", ".join(BROWSER_ROOTS))
        item = getattr(browser, parts[0])
        for p in parts[1:]:
            match = None
            for c in item.children:
                if c.name == p:
                    match = c
                    break
            if match is None:
                raise ValueError("'%s' not found under '%s'" % (p, item.name))
            item = match
        return item

    def cmd_browse(self, path=""):
        browser = self._app().browser
        if not path.strip("/"):
            return [{"name": r, "path": r, "is_folder": True, "is_loadable": False}
                    for r in BROWSER_ROOTS if hasattr(browser, r)]
        base = path.strip("/")
        return [self._item_info(c, base + "/" + c.name) for c in self._resolve_path(base).children]

    def cmd_search_browser(self, query, categories=None, max_results=25):
        browser = self._app().browser
        words = [w for w in query.lower().split() if w]
        cats = categories or ["instruments", "drums", "sounds"]
        results = []
        visited = 0
        for cat in cats:
            if not hasattr(browser, cat):
                continue
            stack = [(getattr(browser, cat), cat)]
            while stack and visited < SEARCH_VISIT_LIMIT and len(results) < max_results:
                item, path = stack.pop()
                visited += 1
                for c in item.children:
                    cpath = path + "/" + c.name
                    name = c.name.lower()
                    if c.is_loadable and all(w in name or w in cpath.lower() for w in words):
                        results.append(self._item_info(c, cpath))
                        if len(results) >= max_results:
                            break
                    if c.is_folder:
                        stack.append((c, cpath))
        return {"results": results, "visited": visited,
                "truncated": visited >= SEARCH_VISIT_LIMIT}

    def cmd_load_item(self, track, uri=None, path=None):
        t = self._track(track)
        item = self._browser_cache.get(uri) if uri else None
        if item is None and path:
            item = self._resolve_path(path)
        if item is None:
            raise ValueError("unknown item - browse or search for it first, then pass its uri or path")
        if not item.is_loadable:
            raise ValueError("'%s' is not loadable" % item.name)
        song = self.song()
        song.view.selected_track = t
        self._app().browser.load_item(item)
        return {"loaded": item.name, "track": track}


    # ----------------------------------------------------------- introspection
    # Raw-but-compact facts; the MCP server turns these into LLM-facing views.

    def cmd_song_state(self):
        song = self.song()
        return {"tempo": song.tempo,
                "signature": [song.signature_numerator, song.signature_denominator],
                "is_playing": song.is_playing}

    def cmd_project_snapshot(self, clips=True):
        song = self.song()
        out = self.cmd_ping()
        out["song"] = ix.song_view(song)
        out["tracks"] = [ix.track_view(song, t, tid, kind, clips=clips)
                         for tid, t, kind in ix.all_tracks(song)]
        view = song.view
        out["selected_track"] = next((tid for tid, t, _ in ix.all_tracks(song)
                                      if ix.same(t, view.selected_track)), None)
        detail = view.detail_clip
        out["detail_clip"] = None
        if detail is not None:
            loc = self._locate_clip(detail)
            if loc:
                out["detail_clip"] = "t%d/%s" % (loc["track"], "s%d" % loc["slot"] if "slot" in loc
                                                 else "a%d" % loc["arrangement_index"])
        return out

    def cmd_track_state(self, track_id):
        song = self.song()
        track = ix.resolve_track(song, track_id)
        kind = ("master" if track_id == "m" else "return" if track_id.startswith("r")
                else ix.track_kind(track))
        out = ix.track_view(song, track, track_id, kind)
        out["mixer"] = ix.mixer_view(song, track, track_id, detail=True)
        out["devices"] = [ix.device_tree(d, "%s/d%d" % (track_id, i))
                          for i, d in enumerate(track.devices)]
        out["automated_params"] = ix.automated_params(song, track, track_id)
        return out

    def cmd_device_state(self, device_id):
        song = self.song()
        kind, dev, track = ix.resolve_device(song, device_id)
        if kind == "mixer":
            return {"id": device_id, "name": "Mixer", "class": "Mixer", "type": "mixer",
                    "parameters": ix.mixer_view(song, track, device_id.split("/")[0], detail=True)}
        out = ix.device_tree(dev, device_id)
        out["parameters"] = [ix.param_view(p, "%s/p%d" % (device_id, i))
                             for i, p in enumerate(dev.parameters)]
        return out

    def cmd_clip_state(self, clip_id, envelope_step=1.0):
        song = self.song()
        clip = ix.resolve_clip(song, clip_id)
        tid = clip_id.split("/")[0]
        return ix.clip_detail(song, clip, clip_id, ix.resolve_track(song, tid), tid,
                              envelope_step)

    def cmd_clip_notes(self, clip_id):
        """Notes plus the context needed to place them in song time (and drum pad names)."""
        song = self.song()
        clip = ix.resolve_clip(song, clip_id)
        if not clip.is_midi_clip:
            raise ValueError("%s is an audio clip - use analyze_audio_clip" % clip_id)
        track = ix.resolve_track(song, clip_id.split("/")[0])
        out = ix.clip_brief(clip, clip_id)
        out.update(ix.clip_timing(clip))
        out["notes"] = ix.notes_compact(clip)
        pads = ix.first_drum_rack(track)
        if pads:
            out["drum_pads"] = pads
        out["track_name"] = track.name
        return out

    def cmd_arrangement_notes(self, track_ids=None):
        """Every MIDI arrangement clip's notes, for structure analysis in one round trip."""
        song = self.song()
        out = []
        for tid, t, kind in ix.all_tracks(song):
            if kind != "midi" or (track_ids and tid not in track_ids):
                continue
            clips = []
            for ai, c in enumerate(ix.g(t, "arrangement_clips", []) or []):
                if c.is_midi_clip:
                    d = ix.clip_timing(c)
                    d["id"] = "%s/a%d" % (tid, ai)
                    d["muted"] = bool(ix.g(c, "muted", False))
                    d["notes"] = ix.notes_compact(c)
                    clips.append(d)
            out.append({"id": tid, "name": t.name, "clips": clips,
                        "drum_pads": ix.first_drum_rack(t)})
        return out

    def cmd_meter_sample(self):
        song = self.song()
        return {"time": song.current_song_time, "playing": song.is_playing,
                "levels": [[tid, round(ix.g(t, "output_meter_left", 0.0), 4),
                            round(ix.g(t, "output_meter_right", 0.0), 4)]
                           for tid, t, _ in ix.all_tracks(song)]}

    # Used only by the server's edit-session rollback, never exposed as tools.

    def cmd_delete_track(self, index, expect_name=None):
        t = self._track(index)
        if expect_name is not None and t.name != expect_name:
            raise ValueError("track %d is '%s', expected '%s'" % (index, t.name, expect_name))
        self.song().delete_track(index)
        return {"deleted": index}

    def cmd_delete_device(self, track_id, index, expect_name=None):
        t = ix.resolve_track(self.song(), track_id)
        devices = list(t.devices)
        if not 0 <= index < len(devices):
            raise IndexError("%s has %d devices" % (track_id, len(devices)))
        if expect_name is not None and devices[index].name != expect_name:
            raise ValueError("device %d is '%s', expected '%s'"
                             % (index, devices[index].name, expect_name))
        t.delete_device(index)
        return {"deleted": index}


class _UndoStep(object):
    """Groups several edits into one Ctrl+Z step where the Live version supports it."""

    def __init__(self, song):
        self._song = song

    def __enter__(self):
        if hasattr(self._song, "begin_undo_step"):
            self._song.begin_undo_step()
        return self

    def __exit__(self, *exc):
        if hasattr(self._song, "end_undo_step"):
            self._song.end_undo_step()
        return False
