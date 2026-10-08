"""Simulated-student harness: runs the real tutor Conversation against a DeepSeek-played beginner and writes transcripts.
Usage: python evals/sim.py <iter-label> [track:day:persona ...]   e.g. aws:2:passive lsat:2:struggling quant:1:curious
Uses a throwaway database so real progress is untouched."""
import asyncio, json, os, random, re, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import db  # noqa: E402
db.DB_PATH = Path(tempfile.mkdtemp()) / "sim.db"
import httpx, tutor, progress, prompts  # noqa: E402

PERSONAS = {
    "passive": dict(w={"filler": 5, "idk": 2, "wrong": 2, "right": 2, "question": 1, "garble": 1}, desc="a quiet, polite learner who mostly says okay/sure/yes without engaging"),
    "struggling": dict(w={"filler": 1, "idk": 5, "wrong": 4, "right": 1, "question": 2, "garble": 1}, desc="a learner who finds it hard, often says they do not know, guesses wrong, gets discouraged"),
    "curious": dict(w={"filler": 1, "idk": 1, "wrong": 2, "right": 4, "question": 4, "garble": 1}, desc="an engaged learner who asks why, tries answers, wants depth"),
    "overconfident": dict(w={"filler": 1, "idk": 0, "wrong": 5, "right": 3, "question": 1, "garble": 1}, desc="a learner who answers confidently and quickly but is often wrong or sloppy"),
}
BEHAVIOR = {
    "filler": "Reply with only a short filler such as 'okay', 'sure', 'yeah makes sense', or 'go on'.",
    "idk": "Say you do not know or have no idea, in your own words.",
    "wrong": "Attempt an answer but make it partly or fully wrong, the way a real beginner would.",
    "right": "Answer correctly if a sharp beginner could reason it out from what was just taught; otherwise a reasonable partial answer.",
    "question": "Ask one genuine clarifying question about what the mentor just said.",
    "garble": "Reply naturally, but write it as a speech-to-text engine would transcribe it, with one or two words wrongly heard (similar sounding), no punctuation fuss.",
}

STUDENT_SYS = ("You are role-playing Stanley, a 30-something software professional with a CS master's degree who is a COMPLETE BEGINNER at the subject "
               "being taught, talking to a voice tutor. You are {desc}. Reply in 1 to 2 short spoken sentences, never more than 30 words, no markdown. "
               "You only know what the tutor has told you in this conversation plus general CS knowledge. Never mention that you are role-playing.")


async def chat(messages, max_tokens, temperature, key):
    body = {"model": tutor.MODEL, "messages": messages, "thinking": {"type": "disabled"}, "temperature": temperature, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=90) as c:
        for _ in range(3):
            r = await c.post(tutor.API_URL, headers={"Authorization": f"Bearer {key}"}, json=body)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            await asyncio.sleep(2)
    return "okay"


async def mentor_turn(conv, text, opener=False):
    speech, tags = [], []
    async for ev in conv.respond(text, opener=opener):
        k = ev[0]
        if k == "sentence": speech.append(ev[1])
        elif k == "log": db.log_topic(conv.track, ev[1], ev[2], ev[3] if len(ev) > 3 else 0); tags.append(f"log:{ev[1]}={ev[2]}@L{ev[3] if len(ev) > 3 else 0}")
        elif k == "covered":
            sec = ev[1]; d, sid = (sec.split(":", 1) if sec[:1] == "d" and ":" in sec else (str(conv.day), sec))
            db.mark_covered(conv.track, int(d.lstrip("d")), sid); tags.append(f"covered:{sec}")
        elif k == "todo": db.add_todo(conv.track, conv.day, ev[1], ev[2]); tags.append(f"TODO:{ev[1]} || {ev[2][:160]}")
        elif k == "board": tags.append(f"board:{ev[1]['kind']}:{ev[1]['title']}")
        elif k == "day_done":
            refusal = conv.day_done_refusal()
            if refusal:
                conv.hint = refusal; tags.append("day_done:REFUSED")
            else: db.mark_day(conv.track, conv.day, "done"); tags.append("day_done:OK")
        elif k == "note": db.add_note(conv.track, conv.day, ev[1]); tags.append("note")
        elif k == "error": tags.append("ERROR:" + ev[1])
    return " ".join(speech), tags


async def run(track, day, persona, turns, mode, rng, key):
    for d in range(1, day):
        db.mark_day(track, d, "done")  # earlier days count as finished legacy days
    conv = tutor.Conversation(track, day, mode)
    P = PERSONAS[persona]
    lines, hist = [], []
    text, tags = await mentor_turn(conv, None, opener=True)
    lines.append((f"MENTOR", text, tags)); hist.append(("m", text))
    for i in range(turns):
        beh = rng.choices(list(P["w"]), weights=list(P["w"].values()))[0]
        msgs = [{"role": "system", "content": STUDENT_SYS.format(desc=P["desc"]) + f"\nThis turn: {BEHAVIOR[beh]}"}]
        for who, t in hist[-12:]:
            msgs.append({"role": "user" if who == "m" else "assistant", "content": t})
        reply = await chat(msgs, 90, 1.0, key)
        lines.append((f"STUDENT[{beh}]", reply, [])); hist.append(("s", reply))
        text, tags = await mentor_turn(conv, reply)
        lines.append(("MENTOR", text, tags)); hist.append(("m", text))
        if any(t.startswith("day_done:OK") for t in tags): break
    return conv, lines


async def main():
    label = sys.argv[1]; specs = sys.argv[2:] or ["aws:2:passive", "lsat:2:struggling", "quant:1:curious"]
    key = tutor.api_key(); rng = random.Random(hash(label) & 0xffff)
    turns = int(os.environ.get("SIM_TURNS", "18")); mode = os.environ.get("SIM_MODE", "teach")
    async def one(spec):
        track, day, persona = spec.split(":")
        conv, lines = await run(track, int(day), persona, turns, mode, random.Random(spec + label), key)
        out = [f"# {label} {spec} mode={mode}\n"]
        for who, text, tags in lines:
            out.append(f"**{who}**: {text}")
            if tags: out.append("   `" + " | ".join(tags) + "`")
        left = [x["title"] for x in progress.remaining(track, int(day))]
        out.append(f"\n---\nSECTIONS STILL UNCOVERED: {left}\nOPEN TODOS: {[t['title'] for t in db.open_todos(track)]}\n")
        p = ROOT / "evals" / "out" / f"{label}-{track}{day}-{persona}.md"; p.write_text("\n".join(out)); return p
    for p in await asyncio.gather(*[one(s) for s in specs]): print(p)

asyncio.run(main())
