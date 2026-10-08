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
    meta = _skills_all().get(f"{track}:{day}", {})
    if track == "quant":   # quant weeks are coached task bundles: every section with skills is taught and checked, not just assigned
        meta = {k: {**v, "kind": "teach" if v.get("skills") else v.get("kind", "teach")} for k, v in meta.items()}
    return meta


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
    hollow = db.session_days_since_epoch(track)
    for d in range(1, day):
        status = db.day_status(track, d)
        if status == "done" and d not in touched and d not in hollow:
            continue
        for s in remaining(track, d):
            items.append({"day": d, "id": s["id"], "title": s["title"], "skipped": status != "done" and d not in touched})
    return items[:limit], len(items)


ITEMS_PATH = Path(__file__).resolve().parent.parent / "data" / "items.json"


@lru_cache(maxsize=1)
def _items_all() -> dict:
    try:
        return json.loads(ITEMS_PATH.read_text())
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _items_bad() -> frozenset:
    try:
        return frozenset(json.loads((ITEMS_PATH.parent / "items_bad.json").read_text()))
    except (OSError, ValueError):
        return frozenset()


def item_for(track: str, day: int, section_id: str) -> dict | None:
    key = f"{track}:{day}:{section_id}"
    return None if key in _items_bad() else _items_all().get(key)


_SKIP_SECTION = __import__("re").compile(r"(setup|welcome|orientation|how this plan|archetype|environment|install|logistics|admin)", __import__("re").I)


@lru_cache(maxsize=8)
def _worthy(track: str) -> frozenset:
    """Lower-cased skills from teach sections that are real exam content (never setup, orientation or plan logistics)."""
    out = set()
    for key, secs in _skills_all().items():
        if not key.startswith(track + ":"):
            continue
        try:
            d = content.load_day(track, int(key.split(":")[1]))
        except Exception:  # noqa: BLE001
            d = None
        if d and _SKIP_SECTION.search(d["title"]):
            continue
        for sid, m in secs.items():
            if m.get("kind") == "teach" and not _SKIP_SECTION.search(sid):
                out.update(k.lower() for k in m.get("skills", []))
    return frozenset(out)


_SKIP_TOPIC = __import__("re").compile(r"(setup|install|environment|conda|repo(sitory)?|git\\b|github|pip\\b|orientation|archetype|schedule|logistics|registration|workspace|ide\\b|jupyter|terminal)", __import__("re").I)


def review_worthy(track: str, topic: str) -> bool:
    if _SKIP_TOPIC.search(topic):
        return False
    w = _worthy(track)
    return (not w) or topic.lower() in w or topic.lower() not in {k.lower() for secs in _skills_all().values() for m in secs.values() for k in m.get("skills", [])}
