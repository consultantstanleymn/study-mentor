"""Progress, weak topics and session notes (SQLite, local only)."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mentor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS day_progress (
  track TEXT, day INTEGER, status TEXT, updated REAL, PRIMARY KEY (track, day));
CREATE TABLE IF NOT EXISTS weak (
  track TEXT, topic TEXT, misses INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, last_seen REAL,
  PRIMARY KEY (track, topic));
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, track TEXT, day INTEGER, note TEXT, created REAL);
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


def log_topic(track: str, topic: str, result: str):
    topic = topic.strip()[:80]
    if not topic:
        return
    col = "misses" if result == "miss" else "hits"
    with conn() as c:
        c.execute(
            f"INSERT INTO weak(track,topic,{col},last_seen) VALUES(?,?,1,?) "
            f"ON CONFLICT(track,topic) DO UPDATE SET {col}={col}+1, last_seen=excluded.last_seen",
            (track, topic, time.time()),
        )


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
        c.execute("INSERT INTO notes(track,day,note,created) VALUES(?,?,?,?)", (track, day, note.strip()[:400], time.time()))


def recent_notes(track: str, limit: int = 5) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT day, note, created FROM notes WHERE track=? ORDER BY id DESC LIMIT ?", (track, limit)).fetchall()
    return [dict(r) for r in rows][::-1]


def start_session(track: str, day: int, mode: str) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO sessions(track,day,mode,started) VALUES(?,?,?,?)", (track, day, mode, time.time()))
        return cur.lastrowid


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
