"""Coverage and carry-forward: which lesson sections are done, and what earlier days still owe."""
import json
from functools import lru_cache
from pathlib import Path

import content
import db

SKILLS_PATH = Path(__file__).resolve().parent.parent / "data" / "skills.json"


OPTIONAL = {"sources", "preview", "recap", "lab", "quiz", "references"}


@lru_cache(maxsize=1)
def _skills_all() -> dict:
    try:
        return json.loads(SKILLS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def section_meta(track: str, day: int) -> dict:
    """{section_id: {"kind": teach|assignment|practice, "skills": [..]}} from data/skills.json (empty if not generated)."""
    return _skills_all().get(f"{track}:{day}", {})


def skills_for_day(track: str, day: int) -> list[str]:
    seen, out = set(), []
    for m in section_meta(track, day).values():
        for k in m.get("skills", []):
            if k.lower() not in seen:
                seen.add(k.lower()); out.append(k)
    return out


def sections_of(track: str, day: int) -> list[dict]:
    d = content.load_day(track, day)
    return [{"id": s["id"], "title": s["title"]} for s in d["sections"] if s["id"] not in OPTIONAL] if d else []


def remaining(track: str, day: int) -> list[dict]:
    """Sections not yet covered. Assignment sections count once a homework item exists for the day; practice sections once he has attempted an exam-style item."""
    done = db.covered_sections(track, day)
    meta = section_meta(track, day)
    has_todo = db.has_todo_for_day(track, day)
    out = []
    for s in sections_of(track, day):
        if s["id"] in done:
            continue
        kind = meta.get(s["id"], {}).get("kind", "teach")
        if (kind == "assignment" and has_todo) or (kind == "practice" and "__l3__" in done):
            continue
        out.append(s)
    return out


def backlog(track: str, day: int, limit: int = 8) -> tuple[list[dict], int]:
    """Sections from EARLIER days that were skipped or only partly covered. Days marked done with no coverage
    records are legacy and assumed fully covered. Returns (first `limit` items oldest first, total count)."""
    items = []
    touched = db.days_with_coverage(track)
    for d in range(1, day):
        status = db.day_status(track, d)
        if status == "done" and d not in touched:
            continue
        for s in remaining(track, d):
            items.append({"day": d, "id": s["id"], "title": s["title"], "skipped": status != "done" and d not in touched})
    return items[:limit], len(items)
