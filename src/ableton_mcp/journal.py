"""Edit sessions: group writes, preview them, then commit or roll back.

`JournaledConnection` wraps `AbletonConnection`. Outside a session it is a pass-through.
Inside one, every mutating command first captures what it is about to change and records an
explicit inverse (a closure over the raw connection). Rollback runs the inverses newest-first.
Live's own undo stack is not used: Live merges steps unpredictably and it also contains the
user's manual edits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

MUTATORS = {
    "create_midi_track", "create_audio_track", "set_track_name", "create_clip",
    "create_arrangement_clip", "delete_clip", "set_clip", "add_notes", "remove_notes",
    "set_tempo", "set_time_signature", "load_item",
}


def _cid(args: dict) -> str:
    if args.get("slot") is not None:
        return f"t{args['track']}/s{args['slot']}"
    return f"t{args['track']}/a{args['arrangement_index']}"


def _ref(args: dict) -> dict:
    return {k: args[k] for k in ("track", "slot", "arrangement_index") if args.get(k) is not None}


def _note_key(n: dict) -> tuple:
    return (n["pitch"], round(n["start"], 3), round(n["duration"], 3), round(n["velocity"]))


@dataclass
class Entry:
    what: str
    target: str
    undo: Callable[[Any], None] | None
    details: dict = field(default_factory=dict)


@dataclass
class Journal:
    active: bool = False
    label: str | None = None
    entries: list[Entry] = field(default_factory=list)
    note_baselines: dict = field(default_factory=dict)  # clip ref tuple -> notes before session edits


class JournaledConnection:
    def __init__(self, raw) -> None:
        self.raw = raw
        self.journal = Journal()

    # ---------------------------------------------------------------- session API

    def begin(self, label: str | None) -> dict:
        if self.journal.active:
            raise ValueError(f"an edit session is already open ({self.journal.label!r}); "
                             "commit or roll it back first")
        self.journal = Journal(active=True, label=label or "edit")
        return {"status": "open", "label": self.journal.label}

    def preview(self) -> dict:
        j = self.journal
        if not j.active:
            return {"status": "no open session"}
        changes = [{"n": i + 1, "what": e.what, "target": e.target, "reversible": e.undo is not None,
                    **({"details": e.details} if e.details else {})} for i, e in enumerate(j.entries)]
        note_diffs = []
        for key, before in j.note_baselines.items():
            ref = dict(key)
            try:
                now = self.raw.call("get_notes", **ref)["notes"]
            except Exception as e:  # clip deleted later in the session, etc.
                note_diffs.append({"clip": _cid(ref), "error": str(e)})
                continue
            b, a = {_note_key(n) for n in before}, {_note_key(n) for n in now}
            note_diffs.append({"clip": _cid(ref), "before": len(before), "after": len(now),
                               "added": len(a - b), "removed": len(b - a)})
        irreversible = sum(1 for c in changes if not c["reversible"])
        return {"status": "open", "label": j.label, "changes": changes, "note_diffs": note_diffs,
                "summary": f"{len(changes)} change(s)"
                           + (f", {irreversible} not reversible" if irreversible else "")}

    def commit(self) -> dict:
        if not self.journal.active:
            return {"status": "no open session"}
        n = len(self.journal.entries)
        label = self.journal.label
        self.journal = Journal()
        return {"status": "committed", "label": label, "changes": n}

    def rollback(self) -> dict:
        if not self.journal.active:
            return {"status": "no open session"}
        undone, failed, skipped = 0, [], []
        for e in reversed(self.journal.entries):
            if e.undo is None:
                skipped.append(e.what)
                continue
            try:
                e.undo(self.raw)
                undone += 1
            except Exception as ex:
                failed.append({"what": e.what, "error": str(ex)})
        label = self.journal.label
        self.journal = Journal()
        out = {"status": "rolled back", "label": label, "undone": undone}
        if failed:
            out["failed"] = failed
        if skipped:
            out["not_reversible"] = skipped
        return out

    # ---------------------------------------------------------------- call path

    def call(self, cmd: str, **args):
        if not self.journal.active or cmd not in MUTATORS:
            return self.raw.call(cmd, **args)
        capture = getattr(self, "_capture_" + cmd)
        return capture(args)

    def _record(self, what, target, undo, **details):
        self.journal.entries.append(Entry(what, target, undo, details))

    def _baseline_notes(self, ref: dict) -> list[dict]:
        notes = self.raw.call("get_notes", **ref)["notes"]
        key = tuple(sorted(ref.items()))
        self.journal.note_baselines.setdefault(key, notes)
        return notes

    # ---------------------------------------------------------------- inverses

    def _capture_create_midi_track(self, args, cmd="create_midi_track"):
        res = self.raw.call(cmd, **args)
        idx, name = res["index"], res["name"]
        self._record(f"created track '{name}'", f"t{idx}",
                     lambda raw: raw.call("delete_track", index=idx, expect_name=name))
        return res

    def _capture_create_audio_track(self, args):
        return self._capture_create_midi_track(args, "create_audio_track")

    def _capture_set_track_name(self, args):
        old = self.raw.call("get_track", track=args["track"])["name"]
        res = self.raw.call("set_track_name", **args)
        t = args["track"]
        self._record(f"renamed track '{old}' -> '{args['name']}'", f"t{t}",
                     lambda raw: raw.call("set_track_name", track=t, name=old))
        return res

    def _snapshot_clip(self, ref: dict) -> dict | None:
        try:
            snap = self.raw.call("get_notes", **ref)
        except Exception:
            return None
        return snap

    def _restore_clip(self, raw, ref: dict, snap: dict) -> None:
        props = {"name": snap.get("name"), "looping": snap.get("looping"),
                 "loop_start": snap.get("loop_start"), "loop_end": snap.get("loop_end")}
        if "start_time" in snap:
            loc = raw.call("create_arrangement_clip", track=ref["track"], start_time=snap["start_time"],
                           length=snap["end_time"] - snap["start_time"], name=snap.get("name"))
            ref = {"track": loc["track"], "arrangement_index": loc["arrangement_index"]}
        else:
            raw.call("create_clip", track=ref["track"], slot=ref["slot"],
                     length=snap.get("length", 4.0), name=snap.get("name"), overwrite=True)
        raw.call("set_clip", **ref, **{k: v for k, v in props.items() if v is not None})
        raw.call("add_notes", **ref, notes=snap["notes"], replace=True)

    def _capture_create_clip(self, args):
        ref = {"track": args["track"], "slot": args["slot"]}
        before = self._snapshot_clip(ref) if args.get("overwrite") else None
        res = self.raw.call("create_clip", **args)

        def undo(raw):
            raw.call("delete_clip", **ref)
            if before:
                self._restore_clip(raw, ref, before)

        self._record("created clip" + (" (replacing one)" if before else ""), _cid(ref), undo,
                     length=args.get("length"))
        return res

    def _capture_create_arrangement_clip(self, args):
        res = self.raw.call("create_arrangement_clip", **args)
        track, index, start = res["track"], res["arrangement_index"], args["start_time"]
        self._record(f"created arrangement clip at beat {start}", f"t{track}/a{index}",
                     lambda raw: raw.call("delete_clip", track=track, arrangement_index=index,
                                          expect_start=start),
                     length=args.get("length"))
        return res

    def _capture_delete_clip(self, args):
        ref = _ref(args)
        before = self._snapshot_clip(ref)
        res = self.raw.call("delete_clip", **args)
        undo = (lambda raw: self._restore_clip(raw, ref, before)) if before else None
        self._record("deleted clip" + ("" if before else " (audio - cannot be restored; use Ctrl+Z in Live)"),
                     _cid(ref), undo)
        return res

    def _capture_set_clip(self, args):
        ref = _ref(args)
        old = self.raw.call("get_clip", **ref)
        res = self.raw.call("set_clip", **args)
        props = {k: old[k] for k in ("name", "looping", "loop_start", "loop_end")}
        self._record("changed clip properties", _cid(ref),
                     lambda raw: raw.call("set_clip", **ref, **props),
                     changed={k: args[k] for k in props if args.get(k) is not None})
        return res

    def _capture_add_notes(self, args):
        ref = _ref(args)
        before = self._baseline_notes(ref)
        res = self.raw.call("add_notes", **args)
        self._record(("replaced notes" if args.get("replace") else "added notes"), _cid(ref),
                     lambda raw: raw.call("add_notes", **ref, notes=before, replace=True),
                     added=len(args.get("notes") or []), before=len(before))
        return res

    def _capture_remove_notes(self, args):
        ref = _ref(args)
        before = self._baseline_notes(ref)
        res = self.raw.call("remove_notes", **args)
        self._record("removed notes", _cid(ref),
                     lambda raw: raw.call("add_notes", **ref, notes=before, replace=True))
        return res

    def _capture_set_tempo(self, args):
        old = self.raw.call("song_state")["tempo"]
        res = self.raw.call("set_tempo", **args)
        self._record(f"tempo {old:g} -> {args['bpm']:g}", "song",
                     lambda raw: raw.call("set_tempo", bpm=old))
        return res

    def _capture_set_time_signature(self, args):
        num, den = self.raw.call("song_state")["signature"]
        res = self.raw.call("set_time_signature", **args)
        self._record(f"time signature {num}/{den} -> {args['numerator']}/{args['denominator']}", "song",
                     lambda raw: raw.call("set_time_signature", numerator=num, denominator=den))
        return res

    def _capture_load_item(self, args):
        t = args["track"]
        before = [d["name"] for d in self.raw.call("get_track", track=t)["devices"]]
        res = self.raw.call("load_item", **args)
        after = [d["name"] for d in self.raw.call("get_track", track=t)["devices"]]
        undo = None
        if len(after) == len(before) + 1:
            idx = next((i for i, (a, b) in enumerate(zip(after, before + [None])) if a != b),
                       len(after) - 1)
            name = after[idx]
            undo = lambda raw: raw.call("delete_device", track_id=f"t{t}", index=idx, expect_name=name)  # noqa: E731
        self._record(f"loaded '{res.get('loaded')}'"
                     + ("" if undo else " (replaced a device - not reversible here; use Ctrl+Z in Live)"),
                     f"t{t}", undo)
        return res
