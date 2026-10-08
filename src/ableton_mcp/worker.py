"""Heavy audio jobs (basic-pitch, librosa) run in a child process.

basic-pitch prints to stdout and the MCP server speaks JSON-RPC over stdout, so heavy work
never runs in-process. Several jobs share one process (imports are the slow part), and
`features` results are cached on disk keyed by file, mtime and job parameters.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER_TIMEOUT = 900
CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "analysis"
CACHEABLE = {"features"}


def _cache_key(job: dict) -> str | None:
    if job.get("type") not in CACHEABLE:
        return None
    try:
        mtime = os.path.getmtime(job["audio_path"])
    except OSError:
        return None
    blob = json.dumps({**job, "_mtime": mtime, "_v": 1}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def run_jobs(jobs: list[dict]) -> list:
    results: list = [None] * len(jobs)
    todo = []
    for i, job in enumerate(jobs):
        if job.get("audio_path") and not Path(job["audio_path"]).is_file():
            raise FileNotFoundError(f"audio file not found: {job['audio_path']}")
        key = _cache_key(job)
        cached = CACHE_DIR / f"{key}.json" if key else None
        if cached and cached.exists():
            results[i] = json.loads(cached.read_text())
        else:
            todo.append((i, job, cached))
    if not todo:
        return results
    with tempfile.TemporaryDirectory(prefix="ableton-mcp-") as tmp:
        job_file, out_file = Path(tmp, "jobs.json"), Path(tmp, "out.json")
        job_file.write_text(json.dumps([job for _, job, _ in todo]))
        proc = subprocess.run(
            [sys.executable, "-m", "ableton_mcp.worker", str(job_file), str(out_file)],
            capture_output=True, text=True, timeout=WORKER_TIMEOUT, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0 or not out_file.exists():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            raise RuntimeError("audio worker failed:\n" + "\n".join(tail))
        outputs = json.loads(out_file.read_text())
    for (i, _, cached), res in zip(todo, outputs):
        results[i] = res
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(res))
    return results


def _run(job: dict):
    kind = job.get("type")
    if kind == "transcribe":
        from .transcribe import run_transcription
        return run_transcription(job)
    if kind == "features":
        from .audio_analysis import compute_features
        return compute_features(job)
    raise ValueError(f"unknown job type {kind!r}")


if __name__ == "__main__":
    job_path, out_path = sys.argv[1], sys.argv[2]
    sys.stdout = sys.stderr  # keep stray prints out of the way
    jobs = json.loads(Path(job_path).read_text())
    Path(out_path).write_text(json.dumps([_run(j) for j in jobs]))
