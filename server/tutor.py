"""Conversation with the tutor model: streaming, hidden-tag parsing, sentence splitting."""
import asyncio
import json
import os
import re
from pathlib import Path

import httpx

import prompts

API_URL = os.environ.get("MENTOR_API_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("MENTOR_MODEL", "deepseek-v4-pro")
KEY_FILE = Path(os.environ.get("MENTOR_KEY_FILE", Path.home() / ".config" / "study-mentor" / "deepseek.key"))
MAX_HISTORY = 28  # messages kept besides the system prompt


def api_key() -> str:
    if os.environ.get("MENTOR_API_KEY"):
        return os.environ["MENTOR_API_KEY"].strip()
    return KEY_FILE.read_text().strip()


class TagParser:
    """Splits a token stream into speech text and hidden tags (board, log, day_done, note)."""

    PAIRED = ("board", "note", "todo")
    SELF = ("log", "day_done", "focus", "layout", "covered")

    def __init__(self):
        self.buf = ""

    def feed(self, chunk: str):
        self.buf += chunk
        return list(self._drain(final=False))

    def flush(self):
        return list(self._drain(final=True))

    def _drain(self, final: bool):
        while self.buf:
            i = self.buf.find("<")
            if i == -1:
                yield ("speech", self.buf)
                self.buf = ""
                return
            if i > 0:
                yield ("speech", self.buf[:i])
                self.buf = self.buf[i:]
            m = re.match(r"<(board|note|todo|log|day_done|focus|layout|covered)\b", self.buf)
            if not m:
                # could still be a partial tag name like "<boa"
                if not final and any(t.startswith(self.buf[1:]) for t in self.PAIRED + self.SELF) and len(self.buf) < 12:
                    return
                yield ("speech", "<")
                self.buf = self.buf[1:]
                continue
            name = m.group(1)
            if name in self.SELF:
                end = self.buf.find("/>")
                if end == -1:
                    if final:
                        self.buf = ""
                    return
                tag = self.buf[: end + 2]
                self.buf = self.buf[end + 2:]
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
                if name == "log":
                    yield ("log", attrs.get("topic", ""), attrs.get("result", "hit"))
                elif name == "focus":
                    yield ("focus", attrs.get("nodes", ""))
                elif name == "layout":
                    yield ("layout", attrs.get("board", "normal"))
                elif name == "covered":
                    yield ("covered", attrs.get("section", ""))
                else:
                    yield ("day_done",)
            else:
                close = f"</{name}>"
                end = self.buf.find(close)
                if end == -1:
                    if final:
                        self.buf = ""
                    return
                inner_all = self.buf[: end]
                self.buf = self.buf[end + len(close):]
                head, _, body = inner_all.partition(">")
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', head))
                body = body.replace("\\n", "\n").strip()
                if name == "board":
                    yield ("board", {"title": attrs.get("title", ""), "kind": attrs.get("kind", "points"), "body": body})
                elif name == "todo":
                    yield ("todo", attrs.get("title", ""), body)
                else:
                    yield ("note", body)


_ABBR = re.compile(r"\b(e\.g|i\.e|vs|etc|Mr|Mrs|Dr|St|No|approx)\.$", re.I)
_END = re.compile(r"[.!?]+[\"')\]]*\s+")


class SentenceSplitter:
    """Cuts streamed text into speakable sentences (a sentence ends at . ! ? followed by whitespace)."""

    def __init__(self):
        self.buf = ""

    def feed(self, text: str):
        self.buf += text
        out = []
        while True:
            cut = None
            for m in _END.finditer(self.buf):
                head = self.buf[: m.end()].rstrip()
                if _ABBR.search(head) or re.search(r"\b\d+\.$", head):
                    continue
                cut = m.end()
                break
            if cut is None:
                nl = self.buf.find("\n")
                if nl != -1:
                    cut = nl + 1
                elif len(self.buf) > 260:
                    comma = self.buf.rfind(", ", 0, 260)
                    cut = comma + 2 if comma > 80 else None
            if cut is None:
                break
            sent, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if sent:
                out.append(sent)
        return out

    def flush(self):
        sent, self.buf = self.buf.strip(), ""
        return [sent] if sent else []


def content_title(track: str, day: int) -> str:
    import content
    d = content.load_day(track, day)
    return d["title"] if d else ""


ASSENT = re.compile(r"^(ok(ay)?|sure|yes|yeah|yep|yup|right|got it|go on|continue|sounds good|makes sense|alright|uh[- ]huh|mm+ ?h?m*)[\s.,!]*(sir|mark)?[\s.,!]*$", re.I)
MAX_SENTENCES = 8


class Conversation:
    def _last_session_fact(self) -> str:
        import db
        notes = db.recent_notes(self.track, 1)
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM sessions WHERE track=?", (self.track,)).fetchone()["n"]
        if n <= 1 and not notes:
            return "This is his very first session in this track."
        return "This is NOT the first session. " + (f"Last session debrief: {notes[-1]['note'][:300]}" if notes else "")

    def __init__(self, track: str, day: int, mode: str):
        self.track, self.day, self.mode = track, day, mode
        self.system = prompts.system_prompt(track, day, mode)
        self.messages: list[dict] = []
        self.hint = ""  # one-shot system note for the next turn (e.g. a refused day_done)
        self.assent_run = 0
        self.turns = 0

    def _payload(self, user_text: str | None):
        sysmsg = self.system + "\n\n" + prompts.coverage_block(self.track, self.day) + "\n\n" + prompts.FINAL_REMINDERS
        msgs = [{"role": "system", "content": sysmsg}] + self.messages[-MAX_HISTORY:]
        if user_text is not None:
            hint = ""
            if self.hint:
                hint += "\n\n" + self.hint; self.hint = ""
            if re.search(r"\b(draw|diagram|whiteboard|board|sketch|visuali[sz]e|illustrate|mermaid|picture)\b", user_text, re.I):
                hint = "\n\n(System note: he asked to see it. Emit a NEW <board> tag in this reply, with <layout board=\"wide\"/> first, then walk through it with <focus/> tags.)"
            msgs.append({"role": "user", "content": user_text + hint})
        return {
            "model": MODEL,
            "messages": msgs,
            "stream": True,
            "thinking": {"type": "disabled"},
            "temperature": 0.8,
            "max_tokens": 320 if user_text and user_text.startswith("(Stanley just opened") else 420,
        }

    async def respond(self, user_text: str | None, opener: bool = False):
        """Async generator of events: ('speech', str) | ('sentence', str) | ('board', dict) | ('log', topic, result)
        | ('day_done',) | ('note', str) | ('error', str). Whatever was emitted counts as said (kept in history),
        even if the caller stops early (barge-in)."""
        if opener:
            user_text = "(Stanley just opened the app and is ready. Begin the session now, following the session shape. " + self._last_session_fact() + ")"
        elif user_text is not None and not user_text.startswith("("):
            self.assent_run = self.assent_run + 1 if ASSENT.match(user_text.strip()) else 0
            if self.assent_run >= 2:
                self.hint = ("(System: he has now said only okay/sure/yes " + str(self.assent_run) + " times in a row. That is NOT evidence of understanding. "
                             "Do not advance. Warmly ask him to say the idea back in his own words or predict a tiny case, and make it easy to answer.)")
        parser, splitter = TagParser(), SentenceSplitter()
        spoken: list[str] = []
        truncated = False
        import progress
        grade_task = cover_task = None
        prev = next((m["content"] for m in reversed(self.messages) if m["role"] == "assistant"), "")
        if user_text and not opener and not user_text.startswith("(") and prev.rstrip().endswith("?") and not ASSENT.match(user_text.strip()):
            grade_task = asyncio.create_task(grade_answer(prev, user_text, content_title(self.track, self.day)))
        self.turns += 1
        if not opener and self.turns % 3 == 0:
            left = progress.remaining(self.track, self.day)
            if left:
                recent = "\n".join(m["content"] for m in self.messages[-12:] if m["role"] == "assistant")
                cover_task = asyncio.create_task(judge_sections(left, recent))
        logged = False
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}

        def handle(events):
            for ev in events:
                if ev[0] == "log":
                    nonlocal logged
                    logged = True
                if ev[0] == "speech":
                    yield ("speech", ev[1])
                    for s in splitter.feed(ev[1]):
                        s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                        if s:
                            spoken.append(s)
                            yield ("sentence", s)
                else:
                    yield ev

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=60.0)) as client:
                async with client.stream("POST", API_URL, headers=headers, json=self._payload(user_text)) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "ignore")[:300]
                        yield ("error", f"Model error {r.status_code}: {body}")
                        return
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            ch = json.loads(data)["choices"][0]
                            delta = ch["delta"].get("content") or ""
                            if ch.get("finish_reason") == "length":
                                truncated = True
                        except Exception:
                            continue
                        if delta:
                            for ev in handle(parser.feed(delta)):
                                yield ev
                        if len(spoken) >= (5 if opener else MAX_SENTENCES):
                            truncated = True
                            break
            for ev in handle(parser.flush()):
                yield ev
            for s in ([] if truncated else splitter.flush()):
                s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                if s:
                    spoken.append(s)
                    yield ("sentence", s)
            if grade_task:
                g = await grade_task
                if g and not logged:
                    yield ("log", g[0], g[1])
            if cover_task:
                for sid in await cover_task:
                    yield ("covered", sid)
        except httpx.HTTPError as e:
            yield ("error", f"Network problem talking to the model: {e}")
        finally:
            reply = " ".join(spoken).strip()
            if user_text is not None:
                self.messages.append({"role": "user", "content": user_text})
            if reply:
                self.messages.append({"role": "assistant", "content": reply})


async def debrief(track: str, day: int, messages: list[dict]) -> str:
    """One cheap non-streaming call after a session: what he learned, what he got wrong, where to start next time."""
    convo = "\n".join(f"{'Stanley' if m['role'] == 'user' else 'Mentor'}: {m['content']}" for m in messages[-40:] if not m["content"].startswith("("))
    ask = ("Write a 3 part debrief of this tutoring session for the next session's mentor. Format exactly: "
           "'Learned: <topics he now understands, comma separated>. Mistakes: <concepts he got wrong or was shaky on, or none>. "
           "Start next time with: <one concrete thing>.' Plain text, under 90 words, only what actually happened.")
    body = {"model": MODEL, "messages": [{"role": "system", "content": ask}, {"role": "user", "content": convo}],
            "thinking": {"type": "disabled"}, "temperature": 0.2, "max_tokens": 220}
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(API_URL, headers={"Authorization": f"Bearer {api_key()}"}, json=body)
        return r.json()["choices"][0]["message"]["content"].strip()


async def _quick(system: str, user: str, max_tokens: int = 160) -> str:
    body = {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": max_tokens}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(API_URL, headers={"Authorization": f"Bearer {api_key()}"}, json=body)
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return ""


def _json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


async def grade_answer(question: str, answer: str, lesson_title: str):
    """Strict independent grade of the student's reply to the mentor's last question. Returns (topic, result) or None."""
    sysmsg = ("You grade a beginner's spoken answer (speech-to-text, so ignore small word errors) to a tutor's question. "
              "Reply ONLY JSON: {\"gradable\": true|false, \"topic\": \"2-4 word stable topic name\", \"result\": \"hit\"|\"miss\"}. "
              "gradable=false if the tutor did not ask a question needing knowledge or reasoning, or the reply is just okay/sure/I don't know. "
              "Be strict: result is hit ONLY if every key part is correct; partial, vague or reversed reasoning is miss. "
              f"Lesson context: {lesson_title}.")
    j = _json(await _quick(sysmsg, f"TUTOR QUESTION: {question[-700:]}\nSTUDENT ANSWER: {answer}"))
    if j and j.get("gradable") and j.get("topic") and j.get("result") in ("hit", "miss"):
        return j["topic"], j["result"]
    return None


async def judge_sections(remaining: list[dict], recent_mentor_text: str):
    """Which not-yet-covered sections has the mentor actually taught (substantively) in the recent turns? Returns ids."""
    if not remaining:
        return []
    listing = "\n".join(f"{x['id']}: {x['title']}" for x in remaining)
    sysmsg = ("You check which lesson sections a tutor has ACTUALLY TAUGHT (explained in substance, not just mentioned) in the transcript. "
              "Reply ONLY JSON: {\"covered\": [ids]}. Use only ids from the list; empty list if none.")
    j = _json(await _quick(sysmsg, f"SECTIONS NOT YET COVERED:\n{listing}\n\nTUTOR TRANSCRIPT:\n{recent_mentor_text[-3500:]}", 120))
    ids = {x["id"] for x in remaining}
    return [i for i in (j or {}).get("covered", []) if i in ids]
