"""Progress, weak topics and session notes (SQLite, local only)."""
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ["MENTOR_DB"]) if os.environ.get("MENTOR_DB") else Path(__file__).resolve().parent.parent / "data" / "mentor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS day_progress (
  track TEXT, day INTEGER, status TEXT, updated REAL, PRIMARY KEY (track, day));
CREATE TABLE IF NOT EXISTS weak (
  track TEXT, topic TEXT, misses INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, last_seen REAL,
  PRIMARY KEY (track, topic));
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, track TEXT, day INTEGER, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS coverage (
  track TEXT, day INTEGER, section TEXT, created REAL, PRIMARY KEY (track, day, section));
CREATE TABLE IF NOT EXISTS todos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, track TEXT, day INTEGER, title TEXT, detail TEXT,
  done INTEGER DEFAULT 0, created REAL, done_at REAL);
CREATE TABLE IF NOT EXISTS mastery (
  track TEXT, topic TEXT, level INTEGER DEFAULT 0, next_due REAL, hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
  last_result TEXT, updated REAL, PRIMARY KEY (track, topic));
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, track TEXT, day INTEGER, mode TEXT, started REAL, ended REAL);
"""


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def get_kv(key, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r["v"] if r else default


def set_kv(key, value):
    with conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))


def current_day(track: str) -> int:
    """The day the owner is on: first not-yet-complete day, or an explicit override."""
    override = get_kv(f"day:{track}")
    if override:
        return int(override)
    with conn() as c:
        done = {r["day"] for r in c.execute("SELECT day FROM day_progress WHERE track=? AND status='done'", (track,))}
    d = 1
    while d in done:
        d += 1
    return d


def set_current_day(track: str, day: int):
    set_kv(f"day:{track}", day)


def mark_day(track: str, day: int, status: str = "done"):
    with conn() as c:
        c.execute(
            "INSERT INTO day_progress(track,day,status,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(track,day) DO UPDATE SET status=excluded.status, updated=excluded.updated",
            (track, day, status, time.time()),
        )
    if status == "done":
        set_kv(f"day:{track}", day + 1)


def done_days(track: str) -> list[int]:
    with conn() as c:
        return [r["day"] for r in c.execute("SELECT day FROM day_progress WHERE track=? AND status='done' ORDER BY day", (track,))]


INTERVAL_DAYS = {0: 0.5, 1: 1, 2: 3, 3: 7, 4: 21}
LEVEL_NAMES = {0: "taught", 1: "recognizes", 2: "applies", 3: "exam-style", 4: "professional"}


def _mastery_update(c, track: str, topic: str, result: str, asked_level: int):
    row = c.execute("SELECT level, hits, misses FROM mastery WHERE track=? AND topic=?", (track, topic)).fetchone()
    level, hits, misses = (row["level"], row["hits"], row["misses"]) if row else (0, 0, 0)
    now = time.time()
    asked_level = max(1, min(4, asked_level or max(1, level)))
    if result == "partial":
        due = now + 1 * 86400
    elif result == "hit":
        level, hits = max(level, asked_level), hits + 1
        due = now + INTERVAL_DAYS[level] * 86400
    else:
        level, misses = max(0, min(level, asked_level) - 1), misses + 1
        due = now + 0.5 * 86400
    c.execute("INSERT INTO mastery(track,topic,level,next_due,hits,misses,last_result,updated) VALUES(?,?,?,?,?,?,?,?) "
              "ON CONFLICT(track,topic) DO UPDATE SET level=excluded.level,next_due=excluded.next_due,hits=excluded.hits,"
              "misses=excluded.misses,last_result=excluded.last_result,updated=excluded.updated",
              (track, topic, level, due, hits, misses, result, now))


def due_topics(track: str, limit: int = 4) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT topic, level, misses, last_result FROM mastery WHERE track=? AND next_due<=? "
                         "ORDER BY (last_result='miss') DESC, next_due ASC LIMIT ?", (track, time.time(), limit)).fetchall()
    return [dict(r) for r in rows]


def mastery_map(track: str, limit: int = 30) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT topic, level, hits, misses FROM mastery WHERE track=? ORDER BY updated DESC LIMIT ?", (track, limit)).fetchall()
    return [dict(r) for r in rows]


def readiness(track: str) -> dict:
    """Share of known topics at exam-style (3) or above, plus the average level."""
    rows = mastery_map(track, 500)
    if not rows:
        return {"topics": 0, "avg_level": 0.0, "pro_share": 0.0}
    return {"topics": len(rows), "avg_level": round(sum(r["level"] for r in rows) / len(rows), 2),
            "pro_share": round(sum(1 for r in rows if r["level"] >= 3) / len(rows), 2)}


def log_topic(track: str, topic: str, result: str, level: int = 0):
    topic = topic.strip()[:80]
    if not topic:
        return
    col = "misses" if result == "miss" else "hits"
    with conn() as c:
        if result == "partial":
            _mastery_update(c, track, topic, "partial", level)
            return
        c.execute(
            f"INSERT INTO weak(track,topic,{col},last_seen) VALUES(?,?,1,?) "
            f"ON CONFLICT(track,topic) DO UPDATE SET {col}={col}+1, last_seen=excluded.last_seen",
            (track, topic, time.time()),
        )
        _mastery_update(c, track, topic, result if result in ("partial", "miss") else "hit", level)


def weak_topics(track: str, limit: int = 6) -> list[dict]:
    """Topics missed more than hit, worst first."""
    with conn() as c:
        rows = c.execute(
            "SELECT topic, misses, hits FROM weak WHERE track=? AND misses > hits "
            "ORDER BY (misses - hits) DESC, last_seen DESC LIMIT ?",
            (track, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_note(track: str, day: int, note: str):
    with conn() as c:
        c.execute("INSERT INTO notes(track,day,note,created) VALUES(?,?,?,?)", (track, day, note.strip()[:700], time.time()))


def recent_notes(track: str, limit: int = 5) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT day, note, created FROM notes WHERE track=? ORDER BY id DESC LIMIT ?", (track, limit)).fetchall()
    return [dict(r) for r in rows][::-1]


def start_session(track: str, day: int, mode: str) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO sessions(track,day,mode,started) VALUES(?,?,?,?)", (track, day, mode, time.time()))
        return cur.lastrowid


def session_days_since_epoch(track: str) -> set[int]:
    """Days that had a real session after coverage tracking began (so a 'done' day without coverage is hollow, not legacy)."""
    ep = get_kv("coverage_epoch")
    if ep is None:
        set_kv("coverage_epoch", str(time.time()))
        return set()
    with conn() as c:
        return {r["day"] for r in c.execute("SELECT DISTINCT day FROM sessions WHERE track=? AND started>?", (track, float(ep)))}


def end_session(sid: int):
    with conn() as c:
        c.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), sid))


def stats(track: str) -> dict:
    with conn() as c:
        mins = c.execute(
            "SELECT COALESCE(SUM(ended-started),0) AS s FROM sessions WHERE track=? AND ended IS NOT NULL", (track,)
        ).fetchone()["s"]
        days = c.execute(
            "SELECT DISTINCT date(started,'unixepoch','localtime') AS d FROM sessions WHERE ended IS NOT NULL ORDER BY d DESC"
        ).fetchall()
    streak, today = 0, time.strftime("%Y-%m-%d")
    seen = [r["d"] for r in days]
    cursor = time.time()
    for _ in range(400):
        d = time.strftime("%Y-%m-%d", time.localtime(cursor))
        if d in seen:
            streak += 1
        elif d != today:
            break
        cursor -= 86400
    return {"minutes": round(mins / 60), "streak": streak}


def mark_covered(track: str, day: int, section: str):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO coverage(track,day,section,created) VALUES(?,?,?,?)", (track, day, section.strip(), time.time()))


def covered_sections(track: str, day: int) -> set[str]:
    with conn() as c:
        return {r["section"] for r in c.execute("SELECT section FROM coverage WHERE track=? AND day=?", (track, day))}


def days_with_coverage(track: str) -> set[int]:
    with conn() as c:
        return {r["day"] for r in c.execute("SELECT DISTINCT day FROM coverage WHERE track=?", (track,))}


def day_status(track: str, day: int) -> str | None:
    with conn() as c:
        r = c.execute("SELECT status FROM day_progress WHERE track=? AND day=?", (track, day)).fetchone()
    return r["status"] if r else None


def add_todo(track: str, day: int, title: str, detail: str) -> int | None:
    title = title.strip()[:140]
    if not title:
        return None
    with conn() as c:
        dup = c.execute("SELECT id FROM todos WHERE track=? AND done=0 AND lower(title)=lower(?)", (track, title)).fetchone()
        if dup:
            return None
        return c.execute("INSERT INTO todos(track,day,title,detail,created) VALUES(?,?,?,?,?)",
                         (track, day, title, detail.strip()[:3000], time.time())).lastrowid


def list_todos(include_done: bool = True, done_limit: int = 15) -> dict:
    with conn() as c:
        open_ = [dict(r) for r in c.execute("SELECT * FROM todos WHERE done=0 ORDER BY created ASC, id ASC")]
        done = [dict(r) for r in c.execute("SELECT * FROM todos WHERE done=1 ORDER BY done_at DESC LIMIT ?", (done_limit,))] if include_done else []
    return {"open": open_, "done": done}


def set_todo_done(todo_id: int, done: bool):
    with conn() as c:
        c.execute("UPDATE todos SET done=?, done_at=? WHERE id=?", (1 if done else 0, time.time() if done else None, todo_id))


def open_todos(track: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT title, day FROM todos WHERE track=? AND done=0 ORDER BY created", (track,))]


def has_todo_for_day(track: str, day: int) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM todos WHERE track=? AND day=? LIMIT 1", (track, day)).fetchone() is not None
