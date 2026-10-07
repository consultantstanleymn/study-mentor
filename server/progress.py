"""Coverage and carry-forward: which lesson sections are done, and what earlier days still owe."""
import content
import db


OPTIONAL = {"sources", "preview", "recap", "lab", "quiz", "references"}


def sections_of(track: str, day: int) -> list[dict]:
    d = content.load_day(track, day)
    return [{"id": s["id"], "title": s["title"]} for s in d["sections"] if s["id"] not in OPTIONAL] if d else []


def remaining(track: str, day: int) -> list[dict]:
    done = db.covered_sections(track, day)
    return [s for s in sections_of(track, day) if s["id"] not in done]


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
