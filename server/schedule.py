"""Reminder schedule API shared with the desktop crab widget."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = Path.home() / ".config" / "study-mentor" / "schedule.json"
TRACKS = {"aws", "lsat", "quant"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT = {
    "reminders": [
        {"id": "aws", "time": "19:30", "days": [0, 1, 2, 3, 4], "track": "aws", "enabled": True},
        {"id": "lsat", "time": "20:30", "days": [0, 1, 2, 3, 4], "track": "lsat", "enabled": False},
        {"id": "quant", "time": "21:00", "days": [0, 1, 2, 3, 4], "track": "quant", "enabled": False},
    ],
    "snooze_min": 10,
}

router = APIRouter()


def _load() -> dict:
    if not SCHEDULE.exists():
        _write(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    return json.loads(SCHEDULE.read_text())


def _write(data: dict) -> None:
    SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SCHEDULE.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SCHEDULE)


def _validate(data: dict) -> dict:
    try:
        snooze = int(data["snooze_min"])
        if not 1 <= snooze <= 120:
            raise ValueError("snooze_min must be 1-120")
        out = []
        for r in data["reminders"]:
            if not TIME_RE.match(str(r["time"])):
                raise ValueError("time must be HH:MM")
            days = r["days"]
            if not isinstance(days, list) or any(not isinstance(d, int) or not 0 <= d <= 6 for d in days):
                raise ValueError("days must be a list of 0-6")
            if r["track"] not in TRACKS:
                raise ValueError("unknown track")
            out.append({"id": str(r["id"])[:32], "time": r["time"], "days": sorted(set(days)),
                        "track": r["track"], "enabled": bool(r["enabled"])})
    except (KeyError, TypeError) as e:
        raise ValueError(f"bad schedule: {e}")
    return {"reminders": out, "snooze_min": snooze}


@router.get("/api/schedule")
async def get_schedule():
    return _load()


@router.post("/api/schedule")
async def set_schedule(data: dict):
    try:
        clean = _validate(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    _write(clean)
    return clean


@router.post("/api/crab-test")
async def crab_test(data: dict | None = None):
    track = (data or {}).get("track", "aws")
    if track not in TRACKS:
        raise HTTPException(422, "unknown track")
    subprocess.Popen(
        [str(ROOT / "venv/bin/python"), str(ROOT / "widget/crab_widget.py"), "--test", track],
        cwd=ROOT, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}
