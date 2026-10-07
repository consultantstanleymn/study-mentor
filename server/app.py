"""Study Mentor: local voice tutor server (FastAPI). Run with run.sh, then open http://127.0.0.1:8765"""
import asyncio
import base64
import json
import time
import threading
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import content
import db
import progress
import tutor
import voice
from schedule import router as schedule_router

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Study Mentor")
app.include_router(schedule_router)


@app.on_event("startup")
async def _startup():
    threading.Thread(target=voice.warm_up, daemon=True).start()


# ---------------- REST ----------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def _convo_key(track, day):
    return f"convo:{track}:{day}"


def _load_convo(track, day):
    raw = db.get_kv(_convo_key(track, day))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if data.get("messages") else None


async def _debrief(track, day, messages):
    try:
        note = await tutor.debrief(track, day, messages)
        if note:
            db.add_note(track, day, note)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/coverage/{track}/{day}")
async def coverage(track: str, day: int):
    items, total = progress.backlog(track, day)
    return {"left_today": [x["title"] for x in progress.remaining(track, day)], "carried": items, "carried_total": total}


@app.get("/api/resume/{track}/{day}")
async def resume_info(track: str, day: int):
    data = _load_convo(track, day)
    if not data:
        return {"available": False}
    return {"available": True, "turns": len(data["messages"]) // 2, "saved": data.get("saved"), "mode": data.get("mode")}


@app.get("/api/state")
async def state():
    tracks = []
    for tid, t in content.TRACKS.items():
        probe = t["dir"] / ("checklist-data.js" if t.get("kind") == "weeks" else "data/days.json")
        if not probe.exists():
            continue
        day = db.current_day(tid)
        meta = content.day_meta(tid, day) or {}
        tracks.append({
            "id": tid,
            "name": t["name"],
            "unit": t.get("unit", "Day"),
            "days": t["days"],
            "day": day,
            "done": db.done_days(tid),
            "title": meta.get("title", ""),
            "phase": meta.get("phase", ""),
            "week": meta.get("week"),
            "weak": db.weak_topics(tid),
            "stats": db.stats(tid),
        })
    return {"tracks": tracks, "voices": voice.VOICES, "voice": voice.DEFAULT_VOICE}


@app.get("/api/outline/{track}")
async def outline(track: str):
    if track not in content.TRACKS:
        return JSONResponse({"error": "unknown track"}, status_code=404)
    return content.outline(track)


@app.get("/api/day/{track}/{day}")
async def day_detail(track: str, day: int):
    d = content.load_day(track, day)
    if not d:
        return JSONResponse({"error": "no such day"}, status_code=404)
    return {"title": d["title"], "phase": d["phase"], "week": d["week"], "services": d["services"],
            "sections": [{"id": s["id"], "title": s["title"]} for s in d["sections"] if s["id"] not in ("recap", "preview", "sources")],
            "questions": len(d["questions"])}


@app.post("/api/day")
async def set_day(body: dict):
    db.set_current_day(body["track"], int(body["day"]))
    return {"ok": True}


@app.post("/api/mark")
async def mark(body: dict):
    db.mark_day(body["track"], int(body["day"]), body.get("status", "done"))
    return {"ok": True, "day": db.current_day(body["track"])}


@app.post("/api/stt")
async def stt(audio: UploadFile = File(...)):
    raw = await audio.read()
    suffix = ".webm" if "webm" in (audio.content_type or "") else ".ogg" if "ogg" in (audio.content_type or "") else ".wav" if "wav" in (audio.content_type or "") else ".webm"
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, voice.transcribe, raw, suffix)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"text": text}


@app.get("/api/tts")
async def tts(text: str, voice_id: str | None = None, speed: float = 1.0):
    loop = asyncio.get_running_loop()
    wav = await loop.run_in_executor(None, voice.synth, text[:600], voice_id, speed)
    return Response(wav, media_type="audio/wav")


# ---------------- conversation socket ----------------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    conv: tutor.Conversation | None = None
    session_id: int | None = None
    settings = {"voice": voice.DEFAULT_VOICE, "speed": 1.0, "board": None}

    def save_convo():
        if conv and conv.messages:
            db.set_kv(_convo_key(conv.track, conv.day), json.dumps(
                {"messages": conv.messages[-60:], "board": settings["board"], "mode": conv.mode, "saved": time.time()}))
    turn = 0
    current: asyncio.Task | None = None
    loop = asyncio.get_running_loop()

    async def send(obj):
        try:
            await sock.send_text(json.dumps(obj))
        except Exception:  # noqa: BLE001
            pass

    async def run_turn(user_text, opener, my_turn):
        """LLM stream -> ordered queue -> TTS -> socket. One queue keeps speech, board and tags in order."""
        q: asyncio.Queue = asyncio.Queue()
        await send({"type": "status", "state": "thinking", "turn": my_turn})

        async def produce():
            try:
                async for ev in conv.respond(user_text, opener=opener):
                    await q.put(ev)
            finally:
                await q.put(None)

        async def consume():
            seq = 0
            while True:
                ev = await q.get()
                if ev is None:
                    break
                kind = ev[0]
                if kind == "sentence":
                    wav = await loop.run_in_executor(None, voice.synth, ev[1], settings["voice"], settings["speed"])
                    await send({"type": "sentence", "turn": my_turn, "seq": seq, "text": ev[1],
                                "audio": base64.b64encode(wav).decode()})
                    seq += 1
                elif kind == "board":
                    settings["board"] = ev[1]
                    await send({"type": "board", "turn": my_turn, **ev[1]})
                elif kind == "layout":
                    await send({"type": "layout", "turn": my_turn, "board": ev[1]})
                elif kind == "focus":
                    await send({"type": "focus", "turn": my_turn, "nodes": ev[1]})
                elif kind == "log":
                    db.log_topic(conv.track, ev[1], ev[2])
                    await send({"type": "log", "turn": my_turn, "topic": ev[1], "result": ev[2]})
                elif kind == "covered":
                    sec = ev[1]
                    d, _, sid = sec.partition(":") if sec[:1] == "d" and ":" in sec else (str(conv.day), "", sec)
                    try:
                        db.mark_covered(conv.track, int(d.lstrip("d")), sid)
                    except ValueError:
                        db.mark_covered(conv.track, conv.day, sec)
                elif kind == "day_done":
                    left = progress.remaining(conv.track, conv.day)
                    if left:
                        conv.hint = ("(System: day_done was REFUSED. These sections are not covered yet: "
                                     + "; ".join(f"[{x['id']}] {x['title']}" for x in left)
                                     + ". Tell him honestly, and teach the next one now.)")
                        await send({"type": "day_blocked", "turn": my_turn, "left": [x["title"] for x in left]})
                    else:
                        db.set_kv(_convo_key(conv.track, conv.day), "")
                        db.mark_day(conv.track, conv.day, "done")
                        await send({"type": "day_done", "turn": my_turn, "day": conv.day})
                        asyncio.create_task(_debrief(conv.track, conv.day, list(conv.messages)))
                elif kind == "note":
                    db.add_note(conv.track, conv.day, ev[1])
                elif kind == "error":
                    await send({"type": "error", "turn": my_turn, "message": ev[1]})

        prod = asyncio.create_task(produce())
        try:
            await consume()
        finally:
            prod.cancel()
        save_convo()
        await send({"type": "done", "turn": my_turn})

    async def cancel_current():
        nonlocal current
        if current and not current.done():
            current.cancel()
            try:
                await current
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    try:
        while True:
            msg = json.loads(await sock.receive_text())
            t = msg.get("type")
            if t == "start":
                await cancel_current()
                if session_id:
                    db.end_session(session_id)
                track, day, mode = msg["track"], int(msg["day"]), msg.get("mode", "teach")
                settings["voice"] = msg.get("voice", settings["voice"])
                settings["speed"] = float(msg.get("speed", settings["speed"]))
                conv = tutor.Conversation(track, day, mode)
                session_id = db.start_session(track, day, mode)
                turn += 1
                current = asyncio.create_task(run_turn(None, True, turn))
            elif t == "resume":
                await cancel_current()
                if session_id:
                    db.end_session(session_id)
                track, day = msg["track"], int(msg["day"])
                saved = _load_convo(track, day)
                settings["voice"] = msg.get("voice", settings["voice"])
                settings["speed"] = float(msg.get("speed", settings["speed"]))
                if not saved:
                    await send({"type": "error", "turn": 0, "message": "Nothing to resume for this day."})
                    continue
                conv = tutor.Conversation(track, day, saved.get("mode", "teach"))
                conv.messages = saved["messages"]
                settings["board"] = saved.get("board")
                session_id = db.start_session(track, day, conv.mode)
                turn += 1
                visible = [{"role": m["role"], "text": m["content"]} for m in conv.messages if not m["content"].startswith("(")]
                await send({"type": "resumed", "turn": turn, "transcript": visible, "board": settings["board"]})
                current = asyncio.create_task(run_turn(
                    "(Stanley is back after a break. Welcome him back in one short sentence, recap in one sentence where you stopped, then continue exactly from there.)",
                    False, turn))
            elif t == "user" and conv:
                await cancel_current()
                turn += 1
                current = asyncio.create_task(run_turn(msg["text"], False, turn))
            elif t == "interrupt":
                await cancel_current()
                turn += 1
                await send({"type": "status", "state": "idle", "turn": turn})
            elif t == "set":
                settings["voice"] = msg.get("voice", settings["voice"])
                settings["speed"] = float(msg.get("speed", settings["speed"]))
            elif t == "end":
                await cancel_current()
                save_convo()
                if conv and sum(1 for m in conv.messages if m["role"] == "user") >= 3:
                    asyncio.create_task(_debrief(conv.track, conv.day, list(conv.messages)))
                if session_id:
                    db.end_session(session_id)
                    session_id = None
                await send({"type": "ended"})
    except WebSocketDisconnect:
        pass
    finally:
        await cancel_current()
        if session_id:
            db.end_session(session_id)


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app.mount("/static", NoCacheStatic(directory=STATIC), name="static")
