"""Minimal stand-ins for Live Object Model objects (only what introspect.py reads)."""
from __future__ import annotations


class Note:
    def __init__(self, pitch, start, duration=0.25, velocity=100, mute=False):
        self.pitch, self.start_time, self.duration = pitch, start, duration
        self.velocity, self.mute = velocity, mute


class Param:
    def __init__(self, name, value, mn=0.0, mx=1.0, default=None, quantized=False, items=(),
                 automation_state=0, fmt=None):
        self.name, self.value, self.min, self.max = name, value, mn, mx
        if default is not None:
            self.default_value = default
        self.is_quantized, self.value_items = quantized, list(items)
        self.automation_state, self._fmt = automation_state, fmt

    def str_for_value(self, v):
        return self._fmt(v) if self._fmt else f"{v:.2f}"


class Routing:
    def __init__(self, name):
        self.display_name = name


class Chain:
    def __init__(self, name, devices, mute=False):
        self.name, self.devices, self.mute = name, devices, mute


class Pad:
    def __init__(self, note, name, chains=()):
        self.note, self.name, self.chains = note, name, list(chains)


class Device:
    def __init__(self, name, cls, dtype=1, params=(), chains=(), pads=None, active=True):
        self.name, self.class_name, self.class_display_name = name, cls, cls
        self.type, self.is_active, self.parameters = dtype, active, list(params)
        self.can_have_chains = bool(chains) or pads is not None
        self.can_have_drum_pads = pads is not None
        self.chains = list(chains)
        if pads is not None:
            self.drum_pads = pads


class Mixer:
    def __init__(self, sends=0):
        self.volume = Param("Track Volume", 0.85, fmt=lambda v: "0.0 dB" if v >= 0.85 else "-12.0 dB")
        self.panning = Param("Track Panning", 0.0, -1.0, 1.0, fmt=lambda v: "C" if v == 0 else f"{v:+.0%}")
        self.sends = [Param(f"Send {i}", 0.0, fmt=lambda v: "-inf dB" if v == 0 else "-10.0 dB")
                      for i in range(sends)]
        self.track_activator = Param("Speaker On", 1.0, quantized=True, items=("Off", "On"))


class Envelope:
    def __init__(self, fn):
        self.fn = fn

    def value_at_time(self, t):
        return self.fn(t)


class Clip:
    def __init__(self, name, midi=True, length=4.0, start=None, notes=(), looping=False,
                 envelopes=None):
        self.name, self.is_midi_clip, self.is_audio_clip = name, midi, not midi
        self.length, self.looping, self.muted = length, looping, False
        self.loop_start, self.loop_end = 0.0, length
        self.start_marker, self.end_marker = 0.0, length
        self.is_arrangement_clip = start is not None
        if start is not None:
            self.start_time, self.end_time = start, start + length
        self._notes = list(notes)
        self._env = envelopes or {}

    def get_notes_extended(self, *a):
        return list(self._notes)

    def automation_envelope(self, param):
        return self._env.get(param.name)


class Slot:
    def __init__(self, clip=None):
        self.clip, self.has_clip = clip, clip is not None


class Track:
    def __init__(self, name, kind="midi", devices=(), session=(), arrangement=(), sends=0,
                 routing=True):
        self.name, self.devices = name, list(devices)
        self.has_midi_input, self.has_audio_input = kind == "midi", kind == "audio"
        self.is_foldable = kind == "group"
        self.mute = self.solo = self.arm = False
        self.can_be_armed = True
        self.mixer_device = Mixer(sends)
        self.clip_slots = [Slot(c) for c in session] + [Slot()]
        self.arrangement_clips = list(arrangement)
        if routing:
            self.input_routing_type = Routing("All Ins")
            self.output_routing_type = Routing("Master")


class CuePoint:
    def __init__(self, name, time):
        self.name, self.time = name, time


class Scene:
    def __init__(self, name):
        self.name = name


class Song:
    def __init__(self, tracks, returns=(), cues=(), scenes=2):
        self.tracks, self.return_tracks = list(tracks), list(returns)
        self.master_track = Track("Master", "audio", routing=False)
        self.cue_points = list(cues)
        self.scenes = [Scene("") for _ in range(scenes)]
        self.tempo, self.signature_numerator, self.signature_denominator = 120.0, 4, 4
        self.is_playing, self.current_song_time, self.song_length = False, 0.0, 64.0
        self.loop, self.loop_start, self.loop_length = False, 0.0, 16.0
        self.root_note, self.scale_name = 2, "Minor"
