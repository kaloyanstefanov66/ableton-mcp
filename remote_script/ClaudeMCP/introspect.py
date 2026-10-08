"""Read-only views of Live Object Model objects, shaped for the MCP server.

Pure functions over LOM objects (no `import Live`), so they can be unit-tested with stubs.
Every attribute read is guarded: Lite and older Live versions lack some properties.

Stable string IDs (positional - valid until tracks/devices are added or removed):
    t3            track 3            r0  return track A      m  master
    t3/s2         session clip       t3/a0  arrangement clip (ordered by start time)
    t3/d1         device             t3/d1/c0/d2  device inside rack chain 0
    t3/d1/n36/d0  device inside the Drum Rack pad for note 36
    t3/mx         the track's mixer (pseudo device)
    <device>/p5   device parameter   t3/mx/vol, t3/mx/pan, t3/mx/send0, t3/mx/on
"""
from __future__ import absolute_import, print_function, unicode_literals

DEVICE_TYPES = {0: "unknown", 1: "instrument", 2: "audio_effect", 4: "midi_effect"}
AUTOMATION_STATES = {1: "automated", 2: "overridden"}
WARP_MODES = {0: "beats", 1: "tones", 2: "texture", 3: "re-pitch", 4: "complex",
              5: "rex", 6: "complex_pro"}
MONITORING = {0: "in", 1: "auto", 2: "off"}
MAX_ENVELOPE_PARAMS = 400


def g(obj, attr, default=None):
    """getattr that also swallows Live's RuntimeErrors for properties a version lacks."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def same(a, b):
    try:
        return a == b
    except Exception:
        return a is b


def r4(x):
    try:
        return round(float(x), 4)
    except Exception:
        return None


# ----------------------------------------------------------------- resolving IDs

def _parts(xid):
    parts = [p for p in str(xid).strip().strip("/").split("/") if p]
    if not parts:
        raise ValueError("empty id")
    return parts


def _index(token, prefix):
    if not token.startswith(prefix) or not token[len(prefix):].isdigit():
        raise ValueError("bad id segment %r (expected %s<number>)" % (token, prefix))
    return int(token[len(prefix):])


def resolve_track(song, tid):
    tid = tid.strip()
    if tid == "m":
        return song.master_track
    if not tid or tid[0] not in "tr" or not tid[1:].isdigit():
        raise ValueError("bad track id %r (use t0.., r0.. or m)" % tid)
    seq = list(song.tracks if tid[0] == "t" else song.return_tracks)
    i = int(tid[1:])
    if not 0 <= i < len(seq):
        raise IndexError("%s does not exist (the set has %d %s)"
                         % (tid, len(seq), "tracks" if tid[0] == "t" else "return tracks"))
    return seq[i]


def resolve_clip(song, cid):
    parts = _parts(cid)
    if len(parts) != 2:
        raise ValueError("bad clip id %r (use t3/s0 or t3/a0)" % cid)
    track = resolve_track(song, parts[0])
    token = parts[1]
    if token.startswith("s"):
        slots = list(track.clip_slots)
        i = _index(token, "s")
        if not 0 <= i < len(slots) or not slots[i].has_clip:
            raise ValueError("%s is empty" % cid)
        return slots[i].clip
    if token.startswith("a"):
        clips = list(g(track, "arrangement_clips", []) or [])
        i = _index(token, "a")
        if not 0 <= i < len(clips):
            raise IndexError("%s does not exist (%s has %d arrangement clips)"
                             % (cid, parts[0], len(clips)))
        return clips[i]
    raise ValueError("bad clip id %r" % cid)


def mixer_params(song, track):
    """[(key, label, parameter)] for a track's mixer."""
    mixer = track.mixer_device
    out = [("vol", "Volume", mixer.volume), ("pan", "Pan", mixer.panning)]
    returns = list(song.return_tracks)
    for i, send in enumerate(g(mixer, "sends", []) or []):
        name = returns[i].name if i < len(returns) else "Send %d" % i
        out.append(("send%d" % i, "Send " + name, send))
    activator = g(mixer, "track_activator")
    if activator is not None:
        out.append(("on", "Track On", activator))
    return out


def resolve_device(song, did):
    """Return (kind, obj, track): kind is 'mixer' (obj = track) or 'device'."""
    parts = _parts(did)
    track = resolve_track(song, parts[0])
    if len(parts) < 2:
        raise ValueError("bad device id %r (use t3/d0 or t3/mx)" % did)
    if parts[1] == "mx":
        return "mixer", track, track
    devices = list(track.devices)
    dev = None
    for token in parts[1:]:
        if token.startswith("p") or token in ("vol", "pan", "on") or token.startswith("send"):
            break
        if token.startswith("d"):
            i = _index(token, "d")
            if not 0 <= i < len(devices):
                raise IndexError("device %s not found in %s" % (token, did))
            dev = devices[i]
        elif token.startswith("c"):
            chains = list(g(dev, "chains", []) or [])
            i = _index(token, "c")
            if dev is None or not 0 <= i < len(chains):
                raise IndexError("chain %s not found in %s" % (token, did))
            devices = list(chains[i].devices)
        elif token.startswith("n"):
            note = _index(token, "n")
            pad = None
            for p in g(dev, "drum_pads", []) or []:
                if p.note == note:
                    pad = p
                    break
            chains = list(g(pad, "chains", []) or [])
            if not chains:
                raise IndexError("drum pad %d is empty in %s" % (note, did))
            devices = list(chains[0].devices)
        else:
            raise ValueError("bad device id segment %r" % token)
    if dev is None:
        raise ValueError("bad device id %r" % did)
    return "device", dev, track


def resolve_param(song, pid):
    parts = _parts(pid)
    kind, obj, track = resolve_device(song, "/".join(parts[:-1]))
    last = parts[-1]
    if kind == "mixer":
        for key, _, p in mixer_params(song, track):
            if key == last:
                return p
        raise ValueError("unknown mixer parameter %r (vol, pan, send0.., on)" % last)
    params = list(obj.parameters)
    i = _index(last, "p")
    if not 0 <= i < len(params):
        raise IndexError("%s has %d parameters" % ("/".join(parts[:-1]), len(params)))
    return params[i]


# --------------------------------------------------------------------- views

def display(p, value=None):
    v = g(p, "value") if value is None else value
    try:
        return p.str_for_value(v)
    except Exception:
        try:
            return str(p)
        except Exception:
            return "%.3f" % v


def param_view(p, pid, detail=True):
    v = g(p, "value", 0.0)
    d = {"id": pid, "name": g(p, "name"), "display": display(p, v)}
    if detail:
        d["value"] = r4(v)
        d["min"] = r4(g(p, "min"))
        d["max"] = r4(g(p, "max"))
        if g(p, "is_quantized", False):
            items = list(g(p, "value_items", []) or [])
            if items:
                d["options"] = [str(x) for x in items[:24]]
        default = g(p, "default_value")
        if default is not None and not g(p, "is_quantized", False):
            d["default"] = r4(default)
            d["changed"] = abs(float(v) - float(default)) > 1e-6
    state = AUTOMATION_STATES.get(g(p, "automation_state", 0))
    if state:
        d["automation"] = state
    if g(p, "is_enabled", True) is False:
        d["enabled"] = False
    return d


def routing_view(track):
    def name(attr):
        return g(g(track, attr), "display_name")

    out = {}
    src = [x for x in (name("input_routing_type"), name("input_routing_channel")) if x]
    dst = [x for x in (name("output_routing_type"), name("output_routing_channel")) if x]
    if src:
        out["input"] = " / ".join(src)
    if dst:
        out["output"] = " / ".join(dst)
    mon = MONITORING.get(g(track, "current_monitoring_state"))
    if mon and src:
        out["monitoring"] = mon
    return out


def mixer_view(song, track, tid, detail=False):
    params = mixer_params(song, track)
    if detail:
        return [param_view(p, "%s/mx/%s" % (tid, key), detail=False) for key, _, p in params]
    out = {}
    for key, label, p in params:
        if key == "vol":
            out["volume"] = display(p)
        elif key == "pan":
            out["pan"] = display(p)
        elif key.startswith("send"):
            out.setdefault("sends", {})[label[5:]] = display(p)
    return out


def device_brief(dev, did):
    d = {"id": did, "name": g(dev, "name"),
         "class": g(dev, "class_display_name") or g(dev, "class_name"),
         "type": DEVICE_TYPES.get(g(dev, "type", 0), "unknown")}
    if not g(dev, "is_active", True):
        d["on"] = False
    if g(dev, "can_have_drum_pads", False):
        d["rack"] = "drum_rack"
    elif g(dev, "can_have_chains", False):
        d["rack"] = "rack"
        d["chains"] = len(list(g(dev, "chains", []) or []))
    return d


def drum_pads(dev):
    """{note: pad name} for pads that hold something."""
    out = {}
    for pad in g(dev, "drum_pads", []) or []:
        if list(g(pad, "chains", []) or []):
            out[int(pad.note)] = g(pad, "name")
    return out


def device_tree(dev, did, depth=2):
    d = device_brief(dev, did)
    if g(dev, "can_have_drum_pads", False):
        pads = []
        for pad in g(dev, "drum_pads", []) or []:
            chains = list(g(pad, "chains", []) or [])
            if chains:
                pads.append({"note": int(pad.note), "name": g(pad, "name"),
                             "id": "%s/n%d" % (did, pad.note),
                             "devices": [g(x, "name") for x in chains[0].devices]})
        d["pads"] = pads
    elif g(dev, "can_have_chains", False) and depth > 0:
        chains = []
        for ci, chain in enumerate(g(dev, "chains", []) or []):
            cid = "%s/c%d" % (did, ci)
            c = {"id": cid, "name": g(chain, "name"),
                 "devices": [device_tree(x, "%s/d%d" % (cid, xi), depth - 1)
                             for xi, x in enumerate(chain.devices)]}
            if g(chain, "mute", False):
                c["mute"] = True
            chains.append(c)
        d["chains"] = chains
    return d


def automated_params(song, track, tid):
    """Parameters on the mixer and top-level devices that carry arrangement automation."""
    out = []
    for key, label, p in mixer_params(song, track):
        state = AUTOMATION_STATES.get(g(p, "automation_state", 0))
        if state:
            out.append({"id": "%s/mx/%s" % (tid, key), "name": label, "device": "Mixer",
                        "state": state})
    for di, dev in enumerate(track.devices):
        for pi, p in enumerate(g(dev, "parameters", []) or []):
            state = AUTOMATION_STATES.get(g(p, "automation_state", 0))
            if state:
                out.append({"id": "%s/d%d/p%d" % (tid, di, pi), "name": g(p, "name"),
                            "device": g(dev, "name"), "state": state})
    return out


def clip_brief(clip, cid):
    d = {"id": cid, "name": g(clip, "name"),
         "kind": "midi" if g(clip, "is_midi_clip", False) else "audio",
         "length": r4(g(clip, "length"))}
    if g(clip, "is_arrangement_clip", False):
        d["start"] = r4(g(clip, "start_time"))
        d["end"] = r4(g(clip, "end_time"))
    if g(clip, "looping", False):
        d["looping"] = True
    if g(clip, "muted", False):
        d["muted"] = True
    return d


def track_kind(track, is_return=False, is_master=False):
    if is_master:
        return "master"
    if is_return:
        return "return"
    if g(track, "is_foldable", False):
        return "group"
    if g(track, "has_midi_input", False):
        return "midi"
    if g(track, "has_audio_input", False):
        return "audio"
    return "unknown"


def track_index(song, track):
    for i, t in enumerate(song.tracks):
        if same(t, track):
            return i
    return None


def track_view(song, track, tid, kind, clips=True):
    d = {"id": tid, "name": track.name, "kind": kind}
    if g(track, "is_grouped", False):
        parent = track_index(song, g(track, "group_track"))
        if parent is not None:
            d["group"] = "t%d" % parent
    for flag in ("mute", "solo"):
        if kind != "master" and g(track, flag, False):
            d[flag] = True
    if g(track, "can_be_armed", False) and g(track, "arm", False):
        d["arm"] = True
    color = g(track, "color_index")
    if color is not None:
        d["color_index"] = color
    d["mixer"] = mixer_view(song, track, tid)
    routing = routing_view(track)
    if routing:
        d["routing"] = routing
    d["devices"] = [device_brief(x, "%s/d%d" % (tid, i)) for i, x in enumerate(track.devices)]
    if clips and kind not in ("master", "return"):
        session = []
        for si, cs in enumerate(g(track, "clip_slots", []) or []):
            if cs.has_clip:
                session.append(clip_brief(cs.clip, "%s/s%d" % (tid, si)))
        d["session_clips"] = session
        d["arrangement_clips"] = [clip_brief(c, "%s/a%d" % (tid, ai))
                                  for ai, c in enumerate(g(track, "arrangement_clips", []) or [])]
        playing = g(track, "playing_slot_index", -1)
        if playing is not None and playing >= 0:
            d["playing_slot"] = playing
    return d


def all_tracks(song):
    """[(id, track, kind)] for tracks, returns and master in display order."""
    out = [("t%d" % i, t, track_kind(t)) for i, t in enumerate(song.tracks)]
    out += [("r%d" % i, t, "return") for i, t in enumerate(song.return_tracks)]
    out.append(("m", song.master_track, "master"))
    return out


def song_view(song):
    d = {
        "tempo": r4(song.tempo),
        "signature": [song.signature_numerator, song.signature_denominator],
        "is_playing": g(song, "is_playing", False),
        "position": r4(g(song, "current_song_time", 0.0)),
        "song_length": r4(g(song, "song_length")),
        "loop": {"on": bool(g(song, "loop", False)), "start": r4(g(song, "loop_start", 0.0)),
                 "length": r4(g(song, "loop_length", 0.0))},
    }
    root, scale = g(song, "root_note"), g(song, "scale_name")
    # Live always has a root/scale (default C Major); it only means something with Scale Mode on.
    if root is not None and scale and g(song, "scale_mode", True):
        d["key"] = {"root": int(root), "scale": scale}
    d["locators"] = sorted(({"name": g(c, "name"), "time": r4(g(c, "time"))}
                            for c in g(song, "cue_points", []) or []),
                           key=lambda c: c["time"] or 0)
    scenes = []
    tracks = list(song.tracks)
    for i, sc in enumerate(song.scenes):
        s = {"index": i, "name": g(sc, "name") or ""}
        count = 0
        for t in tracks:
            slots = list(g(t, "clip_slots", []) or [])
            if i < len(slots) and slots[i].has_clip:
                count += 1
        s["clips"] = count
        tempo = g(sc, "tempo")
        enabled = g(sc, "tempo_enabled")
        if tempo is not None and (enabled if enabled is not None else tempo > 0):
            s["tempo"] = r4(tempo)
        scenes.append(s)
    d["scenes"] = scenes
    return d


def notes_compact(clip):
    """[[pitch, start, duration, velocity, muted]] sorted by time."""
    notes = clip.get_notes_extended(0, 128, 0.0, 1.0e6)
    out = [[int(n.pitch), r4(n.start_time), r4(n.duration), r4(n.velocity), 1 if n.mute else 0]
           for n in notes]
    out.sort(key=lambda n: (n[1], n[0]))
    return out


def clip_timing(clip):
    d = {"loop_start": r4(g(clip, "loop_start")), "loop_end": r4(g(clip, "loop_end")),
         "start_marker": r4(g(clip, "start_marker")), "end_marker": r4(g(clip, "end_marker")),
         "looping": bool(g(clip, "looping", False))}
    if g(clip, "is_arrangement_clip", False):
        d["start_time"] = r4(g(clip, "start_time"))
        d["end_time"] = r4(g(clip, "end_time"))
    return d


def _envelope_params(song, track, tid):
    out = [("%s/mx/%s" % (tid, key), "Mixer: " + label, p)
           for key, label, p in mixer_params(song, track)]
    for di, dev in enumerate(track.devices):
        for pi, p in enumerate(g(dev, "parameters", []) or []):
            out.append(("%s/d%d/p%d" % (tid, di, pi), "%s: %s" % (g(dev, "name"), g(p, "name")), p))
    return out[:MAX_ENVELOPE_PARAMS]


def clip_envelopes(song, clip, track, tid, step=1.0, max_points=64):
    """Clip automation envelopes, sampled on a coarse grid and run-length collapsed."""
    if not hasattr(clip, "automation_envelope"):
        return []
    start = g(clip, "loop_start", 0.0) if g(clip, "looping", False) else g(clip, "start_marker", 0.0)
    end = g(clip, "loop_end", 0.0) if g(clip, "looping", False) else g(clip, "end_marker", 0.0)
    span = max(0.0, (end or 0.0) - (start or 0.0))
    step = max(float(step), span / max(1, max_points - 1)) if span else 1.0
    out = []
    for pid, label, p in _envelope_params(song, track, tid):
        try:
            env = clip.automation_envelope(p)
        except Exception:
            env = None
        if env is None:
            continue
        points, last = [], None
        t = start
        while t <= end + 1e-9:
            try:
                v = env.value_at_time(t)
            except Exception:
                break
            if last is None or abs(v - last) > 1e-6:
                points.append([r4(t - start), display(p, v)])
                last = v
            t += step
        out.append({"param": pid, "name": label,
                    "constant": points[0][1] if len(points) == 1 else None,
                    "points": points if len(points) > 1 else None})
    for e in out:
        for k in ("constant", "points"):
            if e[k] is None:
                del e[k]
    return out


def clip_detail(song, clip, cid, track, tid, envelope_step=1.0):
    d = clip_brief(clip, cid)
    d.update(clip_timing(clip))
    for attr, key in (("signature_numerator", "sig_num"), ("signature_denominator", "sig_den"),
                      ("launch_mode", "launch_mode"), ("launch_quantization", "launch_quantization"),
                      ("legato", "legato"), ("color_index", "color_index"),
                      ("is_playing", "is_playing")):
        v = g(clip, attr)
        if v is not None:
            d[key] = v
    if g(clip, "is_midi_clip", False):
        notes = notes_compact(clip)
        d["notes"] = {"count": len(notes)}
        if notes:
            d["notes"]["pitch_range"] = [min(n[0] for n in notes), max(n[0] for n in notes)]
            d["notes"]["span"] = [notes[0][1], max(n[1] + n[2] for n in notes)]
    else:
        sr, length = g(clip, "sample_rate"), g(clip, "sample_length")
        d["audio"] = {
            "file_path": g(clip, "file_path"),
            "warping": bool(g(clip, "warping", False)),
            "warp_mode": WARP_MODES.get(g(clip, "warp_mode"), g(clip, "warp_mode")),
            "gain": g(clip, "gain_display_string"),
            "transpose": [g(clip, "pitch_coarse", 0), r4(g(clip, "pitch_fine", 0))],
            "sample_rate": sr,
            "duration_s": r4(float(length) / sr) if sr and length else None,
            "warp_markers": len(list(g(clip, "warp_markers", []) or [])),
        }
    d["envelopes"] = clip_envelopes(song, clip, track, tid, envelope_step)
    return d


def first_drum_rack(track):
    """Pad map of the first Drum Rack on a track (top level or one rack deep)."""
    for dev in track.devices:
        if g(dev, "can_have_drum_pads", False):
            return drum_pads(dev)
        for chain in g(dev, "chains", []) or []:
            for inner in chain.devices:
                if g(inner, "can_have_drum_pads", False):
                    return drum_pads(inner)
    return None
