"""Audio -> notes with Spotify's basic-pitch (runs in the worker process, see worker.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path


def transcribe_seconds(audio_path: str, start_s: float = 0.0, end_s: float | None = None,
                       onset_threshold: float = 0.5, frame_threshold: float = 0.3,
                       min_note_ms: float = 80.0, min_freq_hz: float | None = 60.0,
                       max_freq_hz: float | None = None) -> list[dict]:
    """Return notes with start/end in seconds of the source file (not of the excerpt)."""
    from .worker import run_jobs

    if not Path(audio_path).is_file():
        raise FileNotFoundError(f"audio file not found: {audio_path}")
    job = {
        "type": "transcribe", "audio_path": audio_path, "start_s": start_s, "end_s": end_s,
        "onset_threshold": onset_threshold, "frame_threshold": frame_threshold,
        "min_note_ms": min_note_ms, "min_freq_hz": min_freq_hz, "max_freq_hz": max_freq_hz,
    }
    return run_jobs([job])[0]


def run_transcription(job: dict) -> list[dict]:
    import soundfile as sf
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import Model, predict

    info = sf.info(job["audio_path"])
    sr = info.samplerate
    start_s = max(0.0, float(job["start_s"] or 0.0))
    end_s = job["end_s"]
    start = int(start_s * sr)
    stop = int(end_s * sr) if end_s is not None else None
    if start >= info.frames:
        raise ValueError(f"start {start_s:.2f}s is past the end of the file ({info.duration:.2f}s)")
    data, sr = sf.read(job["audio_path"], start=start, stop=stop, always_2d=True)

    with tempfile.TemporaryDirectory(prefix="ableton-mcp-seg-") as tmp:
        excerpt = Path(tmp, "excerpt.wav")
        sf.write(excerpt, data, sr)
        _, _, events = predict(
            str(excerpt),
            Model(ICASSP_2022_MODEL_PATH),
            onset_threshold=job["onset_threshold"],
            frame_threshold=job["frame_threshold"],
            minimum_note_length=job["min_note_ms"],
            minimum_frequency=job["min_freq_hz"],
            maximum_frequency=job["max_freq_hz"],
            multiple_pitch_bends=False,
            melodia_trick=True,
        )
    notes = [{
        "start_s": round(float(s) + start_s, 4),
        "end_s": round(float(e) + start_s, 4),
        "pitch": int(p),
        "velocity": max(1, min(127, int(round(float(a) * 127)))),
    } for s, e, p, a, *_ in events]
    notes.sort(key=lambda n: (n["start_s"], n["pitch"]))
    return notes
