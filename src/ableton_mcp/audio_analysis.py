"""Audio analysis primitives and performance-timing ("pulse") estimation.

Live's API exposes no sample data, so audio is analyzed from the clip's source file
(clip.file_path) in a worker process; the server maps results into project beats/bars with
the clip's warp mapping. `compute_features` runs inside the worker; everything else is pure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

from .music import PiecewiseLinear

BANDS = (("low", 0, 250), ("mid", 250, 2000), ("high", 2000, 1e9))
SILENCE_DB = -60.0
NOMINAL_TICKS = ((1.0, "1/4"), (0.5, "1/8"), (1 / 3, "1/8T"), (0.25, "1/16"))
FOLLOW_MIN_CONFIDENCE = 0.7


def _db(x: float, floor: float = -120.0) -> float:
    return round(max(floor, 20 * math.log10(x)) if x > 0 else floor, 1)


def compute_features(job: dict) -> dict:
    """Worker side: per-segment levels/spectrum and onset times for one file region."""
    import librosa
    import numpy as np
    import soundfile as sf

    info = sf.info(job["audio_path"])
    sr = info.samplerate
    start_s = max(0.0, float(job.get("start_s") or 0.0))
    end_s = job.get("end_s")
    stop = int(end_s * sr) if end_s is not None else None
    y, sr = sf.read(job["audio_path"], start=int(start_s * sr), stop=stop, always_2d=True,
                    dtype="float32")
    mono = y.mean(axis=1)
    hop, n_fft = 512, 2048
    out = {"duration_s": round(info.duration, 3), "sample_rate": sr, "channels": info.channels,
           "peak_db": _db(float(np.abs(y).max()) if y.size else 0.0),
           "clipped_samples": int((np.abs(y) >= 0.999).sum())}
    if mono.size < n_fft:
        out.update(onsets_s=[], onset_strength=[], segments=[])
        return out
    onset_hop = 256
    env = librosa.onset.onset_strength(y=mono, sr=sr, hop_length=onset_hop)
    frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=onset_hop,
                                        backtrack=False)
    peak_env = float(env.max()) or 1.0
    # Spectral-flux peaks trail the attack by ~1 hop; snap each to the steepest rise of the
    # amplitude envelope just before it, so timing analysis is accurate to a few ms.
    amp = np.convolve(np.abs(mono), np.ones(max(1, int(sr * 0.0015))) / max(1, int(sr * 0.0015)),
                      mode="same")
    rise = np.diff(amp, prepend=amp[0])
    onsets = []
    for t in librosa.frames_to_time(frames, sr=sr, hop_length=onset_hop):
        i0, i1 = max(0, int((t - 0.04) * sr)), min(len(rise), int((t + 0.01) * sr))
        onsets.append((i0 + int(np.argmax(rise[i0:i1]))) / sr if i1 > i0 else float(t))
    out["onsets_s"] = [round(start_s + t, 4) for t in onsets]
    out["onset_strength"] = [round(float(env[f]) / peak_env, 3) for f in frames]
    power = np.abs(librosa.stft(mono, n_fft=n_fft, hop_length=hop)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    centroid = librosa.feature.spectral_centroid(S=np.sqrt(power), sr=sr)[0]
    frame_t = librosa.frames_to_time(np.arange(power.shape[1]), sr=sr, hop_length=hop) + start_s
    band_power = {name: power[(freqs >= lo) & (freqs < hi)].sum(axis=0) for name, lo, hi in BANDS}
    segs = []
    for a, b in job.get("segments_s") or []:
        i0, i1 = int((a - start_s) * sr), int((b - start_s) * sr)
        chunk = y[max(0, i0):max(0, i1)]
        if chunk.size == 0:
            segs.append(None)
            continue
        m = chunk.mean(axis=1)
        sel = (frame_t >= a) & (frame_t < b)
        total = sum(float(band_power[n][sel].sum()) for n, _, _ in BANDS) or 1.0
        seg = {"peak_db": _db(float(np.abs(chunk).max())),
               "rms_db": _db(float(np.sqrt(np.mean(m.astype("float64") ** 2))))}
        for n, _, _ in BANDS:
            seg[f"{n}_pct"] = round(100 * float(band_power[n][sel].sum()) / total)
        seg["centroid_hz"] = round(float(np.median(centroid[sel]))) if sel.any() else None
        segs.append(seg)
    out["segments"] = segs
    return out


# ----------------------------------------------------------------- timing

@dataclass
class Pulse:
    """A performer's pulse: nominal grid positions (where the notes 'should' be at the
    project tempo) mapped to where they were actually played (both in beats)."""
    tick: float
    tick_label: str
    performed_tick: float
    performed_bpm: float
    project_bpm: float
    confidence: float
    nominal: list[float] = field(default_factory=list)
    actual: list[float] = field(default_factory=list)
    skipped: int = 0

    def _map(self, v: float, src: list[float], dst: list[float]) -> float:
        if len(src) < 2:
            return v + (dst[0] - src[0] if src else 0.0)
        if v <= src[0]:
            return v + dst[0] - src[0]
        if v >= src[-1]:
            return v + dst[-1] - src[-1]
        return PiecewiseLinear(src, dst).forward(v)

    def to_actual(self, nominal_beat: float) -> float:
        return self._map(nominal_beat, self.nominal, self.actual)

    def to_nominal(self, actual_beat: float) -> float:
        return self._map(actual_beat, self.actual, self.nominal)

    def drift(self) -> list[tuple[float, float]]:
        """(actual beat, actual - nominal) per pulse; negative = ahead of the grid."""
        return [(a, a - n) for n, a in zip(self.nominal, self.actual)]


def _moving_median(xs: list[float], width: int = 5) -> list[float]:
    h = width // 2
    return [median(xs[max(0, i - h):i + h + 1]) for i in range(len(xs))]


def estimate_pulse(onsets: list[float], project_bpm: float, min_onsets: int = 8) -> Pulse | None:
    """Infer the performed pulse from onset times (beats at the project tempo).

    1. Find the smallest common inter-onset gap (the tick).
    2. Walk the onsets, assigning each an integer tick index (gaps of n ticks count n),
       adapting the tick length as the player drifts. Onsets closer than ~0.6 ticks are
       ornaments and ignored.
    3. Fit a line for the performed tempo, snap the tick to the nearest musical value, and
       keep a smoothed per-pulse offset so mapping follows drift but not jitter.
    """
    b: list[float] = []
    for t in sorted(onsets):
        if not b or t - b[-1] >= 0.08:
            b.append(t)
    if len(b) < min_onsets:
        return None
    gaps = [y - x for x, y in zip(b, b[1:])]
    cand = sorted(g for g in gaps if 0.15 <= g <= 1.5)
    if len(cand) < min_onsets // 2:
        return None
    support = {g: sum(1 for h in cand if abs(h - g) <= 0.08 * g) for g in cand}
    need = max(3, 0.2 * len(cand))
    seed = min((g for g in cand if support[g] >= need), default=max(cand, key=support.get))
    u = median(h for h in cand if abs(h - seed) <= 0.08 * seed)

    ks, kept, good, total, skipped = [0], [b[0]], 0, 0, 0
    for t in b[1:]:
        gap = t - kept[-1]
        ratio = gap / u
        if ratio < 0.6:
            skipped += 1
            continue
        n = max(1, round(ratio))
        err = abs(ratio - n)
        total += 1
        good += err < 0.2
        ks.append(ks[-1] + n)
        kept.append(t)
        if err < 0.2:
            u = 0.85 * u + 0.15 * (gap / n)
    if total == 0:
        return None
    k_mean, t_mean = sum(ks) / len(ks), sum(kept) / len(kept)
    var = sum((k - k_mean) ** 2 for k in ks)
    slope = sum((k - k_mean) * (t - t_mean) for k, t in zip(ks, kept)) / var if var else u
    intercept = t_mean - slope * k_mean
    tick, label = min(NOMINAL_TICKS, key=lambda x: abs(math.log(slope / x[0])))
    resid = _moving_median([t - (intercept + slope * k) for k, t in zip(ks, kept)])
    actual = [intercept + slope * k + r for k, r in zip(ks, resid)]
    g0 = round(kept[0] / tick) * tick
    nominal = [g0 + k * tick for k in ks]
    confidence = (good / total) * min(1.0, len(kept) / 16)
    return Pulse(tick=tick, tick_label=label, performed_tick=slope,
                 performed_bpm=project_bpm * tick / slope, project_bpm=project_bpm,
                 confidence=round(confidence, 2), nominal=nominal, actual=actual, skipped=skipped)


def timing_report(pulse: Pulse | None, bar_of, bpb: float) -> dict:
    if pulse is None:
        return {"available": False, "reason": "too few clear onsets to infer a pulse"}
    ms = 60000 / pulse.project_bpm
    per_bar: dict[int, list[float]] = {}
    for a, d in pulse.drift():
        per_bar.setdefault(bar_of(a), []).append(d)
    bars = sorted(per_bar)
    drift_ms = [round(sum(per_bar[x]) / len(per_bar[x]) * ms) for x in bars]
    worst = max(drift_ms, key=abs) if drift_ms else 0
    pct = (pulse.performed_bpm / pulse.project_bpm - 1) * 100
    end_drift = drift_ms[-1] if drift_ms else 0
    summary = (f"played at ~{pulse.performed_bpm:.1f} BPM vs project {pulse.project_bpm:g} "
               f"({pct:+.1f}%), pulse {pulse.tick_label}; ends {abs(end_drift)} ms "
               f"{'ahead of' if end_drift < 0 else 'behind'} Live's grid")
    return {"available": True, "performed_bpm": round(pulse.performed_bpm, 1),
            "project_bpm": pulse.project_bpm, "tempo_deviation_pct": round(pct, 1),
            "pulse": pulse.tick_label, "confidence": pulse.confidence,
            "followable": pulse.confidence >= FOLLOW_MIN_CONFIDENCE,
            "drift_ms": {"bars": bars, "ms": drift_ms, "legend": "negative = ahead of the grid"},
            "max_drift_ms": worst, "summary": summary}


def per_bar(features: dict, bars: list[int], onset_bars: list[int]) -> dict:
    """Columnar per-bar tables from worker segments (one per bar) and onset bar numbers."""
    segs = features.get("segments") or []
    rows = [(b, s) for b, s in zip(bars, segs) if s]
    counts = {b: 0 for b in bars}
    for ob in onset_bars:
        if ob in counts:
            counts[ob] += 1
    silent = [b for b, s in rows if s["rms_db"] <= SILENCE_DB]
    runs = []
    for b in silent:
        if runs and runs[-1][1] == b - 1:
            runs[-1][1] = b
        else:
            runs.append([b, b])
    return {
        "bars": [b for b, _ in rows],
        "rms_db": [s["rms_db"] for _, s in rows],
        "peak_db": [s["peak_db"] for _, s in rows],
        "onsets": [counts[b] for b, _ in rows],
        "low_pct": [s["low_pct"] for _, s in rows],
        "mid_pct": [s["mid_pct"] for _, s in rows],
        "high_pct": [s["high_pct"] for _, s in rows],
        "centroid_hz": [s["centroid_hz"] for _, s in rows],
        "silent_bars": runs,
    }
