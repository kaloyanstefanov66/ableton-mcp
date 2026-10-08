import random

import numpy as np
import pytest
import soundfile as sf

from ableton_mcp import worker
from ableton_mcp.audio_analysis import estimate_pulse, per_bar, timing_report
from ableton_mcp.refs import bar_of


def performance(bpm=127.0, project=120.0, bars=8, jitter_ms=8, seed=3):
    """A 9/8-style riff (one rest per 9 eighths) played at `bpm`, in project beats.
    Returns (onsets, nominal grid positions of those onsets)."""
    rng = random.Random(seed)
    scale = project / bpm  # one performed beat lasts this many project beats
    onsets, nominal = [], []
    for k in range(bars * 9):
        if k % 9 == 3:
            continue  # the riff's rest
        t = 100.0 + k * 0.5 * scale + rng.uniform(-1, 1) * jitter_ms / 1000 * project / 60
        onsets.append(t)
        nominal.append(100.0 + k * 0.5)
    return onsets, nominal


def test_pulse_recovers_tempo_and_follows_drift():
    onsets, nominal = performance()
    p = estimate_pulse(onsets, 120.0)
    assert p is not None and p.tick_label == "1/8"
    assert abs(p.performed_bpm - 127.0) < 1.0
    assert p.confidence >= 0.8
    # grid-written notes land on the performance within 15 ms
    worst_ms = max(abs(p.to_actual(nm) - on) for nm, on in zip(nominal, onsets)) * 500
    assert worst_ms < 15
    # and the inverse reads the performance back onto the grid
    assert max(abs(p.to_nominal(on) - nm) for nm, on in zip(nominal, onsets)) < 0.05


def test_timing_report_says_ahead_when_rushing():
    onsets, _ = performance(bpm=129.0)
    r = timing_report(estimate_pulse(onsets, 120.0), lambda b: bar_of(b, 4.0), 4.0)
    assert r["available"] and r["followable"]
    assert r["tempo_deviation_pct"] > 6
    assert r["drift_ms"]["ms"][-1] < -1000  # well over a second ahead by the end
    assert "ahead of" in r["summary"]


def test_pulse_needs_enough_onsets():
    assert estimate_pulse([0.0, 0.5, 1.0], 120.0) is None
    assert timing_report(None, lambda b: 1, 4.0)["available"] is False


def test_per_bar_tables_and_silence():
    feats = {"segments": [{"peak_db": -3.0, "rms_db": -12.0, "low_pct": 70, "mid_pct": 20,
                           "high_pct": 10, "centroid_hz": 300},
                          {"peak_db": -90.0, "rms_db": -90.0, "low_pct": 0, "mid_pct": 0,
                           "high_pct": 0, "centroid_hz": None},
                          {"peak_db": -91.0, "rms_db": -95.0, "low_pct": 0, "mid_pct": 0,
                           "high_pct": 0, "centroid_hz": None}]}
    t = per_bar(feats, [5, 6, 7], [5, 5, 6])
    assert t["onsets"] == [2, 1, 0]
    assert t["silent_bars"] == [[6, 7]]


@pytest.fixture
def clicks_wav(tmp_path):
    """2 s of clicks every 0.25 s over a low tone, then 2 s of silence, then 2 s of hiss."""
    sr = 22050
    y = np.zeros(sr * 6, dtype="float32")
    t = np.arange(sr * 2) / sr
    y[: sr * 2] += 0.2 * np.sin(2 * np.pi * 80 * t)
    for k in range(8):
        i = int(k * 0.25 * sr)
        y[i:i + 200] += np.hanning(200).astype("float32") * 0.9
    y[sr * 4:] = np.random.default_rng(0).normal(0, 0.05, sr * 2).astype("float32")
    path = tmp_path / "clicks.wav"
    sf.write(path, y, sr)
    return str(path)


def test_worker_features_on_synthetic_audio(clicks_wav, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path / "cache")
    job = {"type": "features", "audio_path": clicks_wav, "start_s": 0.0, "end_s": None,
           "segments_s": [[0, 2], [2, 4], [4, 6]]}
    f = worker.run_jobs([job])[0]
    assert abs(f["duration_s"] - 6.0) < 0.01
    first = [o for o in f["onsets_s"] if o < 1.9]
    assert 6 <= len(first) <= 9
    assert all(min(abs(o - k * 0.25) for k in range(8)) < 0.01 for o in first)
    tone, silence, hiss = f["segments"]
    assert silence["rms_db"] < -60 < tone["rms_db"]
    assert tone["low_pct"] > tone["high_pct"]
    assert hiss["high_pct"] > hiss["low_pct"]
    # second call is served from the cache without a worker process
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert worker.run_jobs([job])[0] == f
