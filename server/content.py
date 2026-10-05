"""Load the two curricula (AWS SAP-C02, LSAT 170) from the cloned repos into plain structures."""
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent / "content"
TRACKS = {
    "aws": {"name": "AWS Solutions Architect Pro", "dir": ROOT / "aws-sa-pro-curriculum", "days": 70, "unit": "Day"},
    "lsat": {"name": "LSAT 170", "dir": ROOT / "lsat-170-curriculum", "days": 168, "unit": "Day"},
    "quant": {"name": "Quant Finance", "dir": ROOT / "quant-checklist", "days": 24, "unit": "Week", "kind": "weeks"},
}


def _strip_html(h: str) -> str:
    if "<" not in (h or ""):
        return h or ""
    return BeautifulSoup(h, "lxml").get_text(" ", strip=True)


@lru_cache(maxsize=None)
def _quant_weeks() -> dict:
    """The quant checklist is a JS data file (MONTHS = [...]). Evaluate it with node and keep the weeks."""
    src = (TRACKS["quant"]["dir"] / "checklist-data.js").read_text()
    script = "const fs=require('fs');eval(fs.readFileSync(0,'utf8')+';globalThis.M=MONTHS');console.log(JSON.stringify(M))"
    out = subprocess.run(["node", "-e", script], input=src, capture_output=True, text=True, timeout=20)
    months = json.loads(out.stdout)
    weeks = {}
    for m in months:
        for w in m["weeks"]:
            cats = []
            for c in w["categories"]:
                tasks = []
                for t in c["tasks"]:
                    steps = [_strip_html(x["text"]) for x in t.get("steps", [])]
                    links = [f"{l['title']} ({l.get('desc', '')})" for l in t.get("links", [])]
                    tasks.append({"text": t["text"], "note": t.get("note", ""), "tag": t.get("tag", ""), "steps": steps, "links": links})
                cats.append({"label": c["label"], "tasks": tasks})
            weeks[w["week"]] = {
                "day": w["week"], "week": w["week"], "title": w["title"], "date": w.get("date", ""),
                "phase": f"Month {m['num']}: {m['name']}", "month_desc": m.get("desc", ""), "categories": cats,
                "services": [c["label"] for c in cats],
            }
    return weeks


@lru_cache(maxsize=None)
def _days_index(track: str) -> dict:
    if TRACKS[track].get("kind") == "weeks":
        return _quant_weeks()
    data = json.loads((TRACKS[track]["dir"] / "data" / "days.json").read_text())
    return {d["day"]: d for d in data}


def day_meta(track: str, day: int) -> dict | None:
    return _days_index(track).get(day)


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def _node_text(node) -> str:
    """Readable text for a section: paragraphs, list items, table rows."""
    out = []
    for el in node.find_all(["h2", "h3", "p", "li", "tr", "pre"]):
        if el.name in ("h2", "h3"):
            out.append("\n## " + el.get_text(" ", strip=True))
        elif el.name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["th", "td"])]
            out.append(" | ".join(cells))
        elif el.name == "li":
            if el.find_parent("tr") is None:
                out.append("- " + el.get_text(" ", strip=True))
        else:
            if el.find_parent("tr") is None:
                out.append(el.get_text(" ", strip=True))
    return _clean("\n".join(out))


@lru_cache(maxsize=None)
def load_day(track: str, day: int) -> dict | None:
    meta = day_meta(track, day)
    if TRACKS[track].get("kind") == "weeks":
        return _load_quant_week(meta) if meta else None
    path = TRACKS[track]["dir"] / "days" / f"day-{day:03d}.html"
    if not meta or not path.exists():
        return None
    soup = BeautifulSoup(path.read_text(), "lxml")
    sections, questions = [], []
    for sec in soup.select("section.doc-section"):
        sid = sec.get("id", "")
        if sid == "quiz":
            continue
        h = sec.find("h2")
        sections.append({"id": sid, "title": h.get_text(" ", strip=True) if h else sid, "text": _node_text(sec)})
    for card in soup.select(".scenario-card"):
        q = card.select_one(".scenario-question")
        if not q:
            continue
        opts = [o.get_text(" ", strip=True) for o in card.select(".scenario-option")]
        exp = card.select_one(".scenario-explanation")
        btn = card.select_one(".scenario-reveal-btn")
        correct = None
        if btn and btn.get("onclick"):
            m = re.search(r",\s*(\d+)\s*\)", btn["onclick"])
            if m:
                correct = int(m.group(1))
        questions.append({
            "q": re.sub(r"^Q\d+\.\s*", "", q.get_text(" ", strip=True)),
            "options": opts,
            "correct": correct,
            "explanation": exp.get_text(" ", strip=True) if exp else "",
        })
    # AWS days.json also carries a short lab/summary; keep as a fallback
    return {
        "track": track,
        "day": day,
        "title": meta.get("title", f"Day {day}"),
        "phase": meta.get("phase", ""),
        "week": meta.get("week"),
        "summary": meta.get("summary", ""),
        "lab": meta.get("lab", ""),
        "services": meta.get("services", []),
        "sections": sections,
        "questions": questions,
    }


def _load_quant_week(meta: dict) -> dict:
    sections = []
    for c in meta["categories"]:
        lines = []
        for t in c["tasks"]:
            lines.append(f"TASK ({t['tag']}): {t['text']}\n  Why: {t['note']}")
            for i, st in enumerate(t["steps"], 1):
                lines.append(f"  Step {i}: {st}")
            if t["links"]:
                lines.append("  Resources: " + "; ".join(t["links"]))
        sections.append({"id": c["label"].lower().replace(" ", "-"), "title": c["label"], "text": "\n".join(lines)})
    return {
        "track": "quant", "day": meta["day"], "title": meta["title"], "phase": meta["phase"], "week": meta["week"],
        "summary": f"{meta['date']}. {meta['month_desc']}", "lab": "", "services": meta["services"],
        "sections": sections, "questions": [],
    }


def lesson_text(track: str, day: int, max_chars: int = 60000) -> str:
    d = load_day(track, day)
    if not d:
        return ""
    parts = [f"# {TRACKS[track]['name']} - {TRACKS[track]['unit']} {day}: {d['title']}", f"Phase: {d['phase']}"]
    if d["summary"]:
        parts.append("Summary: " + d["summary"])
    for s in d["sections"]:
        parts.append(f"\n### [{s['id']}] {s['title']}\n{s['text']}")
    if d["lab"]:
        parts.append("\n### Lab\n" + d["lab"])
    text = "\n".join(parts)
    return text[:max_chars]


def outline(track: str) -> list[dict]:
    return [
        {"day": d["day"], "title": d.get("title", ""), "phase": d.get("phase", ""), "week": d.get("week")}
        for d in sorted(_days_index(track).values(), key=lambda x: x["day"])
    ]


def trap_library() -> str:
    p = TRACKS["lsat"]["dir"] / "data" / "trap-library.json"
    return p.read_text() if p.exists() else ""
