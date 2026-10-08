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
                    yield ("log", attrs.get("topic", ""), attrs.get("result", "hit"), int(attrs.get("level", "0") or 0) if str(attrs.get("level", "0")).isdigit() else 0)
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


OFFER = re.compile(r"(want (me )?to|want an|ready\?|shall we|should (we|i)|make sense|sound (good|doable|fair)|does that (connect|land|help)|any questions|better\?|clear\?)", re.I)
PRAISE = re.compile(r"^(exactly|perfect|precisely|that'?s (exactly )?right|correct|yes[,.!]|great|spot on|right[,.!])", re.I)
MACHINERY = re.compile(r"\b(mark(ing|ed)?|log(ging|ged)?|record(ing|ed)?|sav(e|ing|ed))\b[^.?!]{0,40}\b(cover(ed)?|section|that|this|answer|correct(ly)?|progress|miss|hit)\b|\bI('ve| have) covered\b|\bsection[^.?!]{0,30}\bcovered\b", re.I)
HW_PROMISE = re.compile(r"\b(I'?ll|I will|I'?ve|I have|let me|I'?m going to|going to)\b[^.?!]{0,40}\b(add|put|added|assign|assigned|give|save|drop)\b[^.?!]{0,50}\b(list|to-?do|homework|assignment)", re.I)
BOARD_PROMISE = re.compile(r"(look at the board|(see|on|check|at) the board|let me draw|i'?ll draw|i'?ll sketch|here'?s the (comparison|diagram|table)|the board shows)", re.I)

ASSENT = re.compile(r"^(ok(ay)?|sure|yes|yeah|yep|yup|right|got it|go on|continue|sounds good|makes sense|alright|uh[- ]huh|mm+ ?h?m*)[\s.,!]*(sir|mark)?[\s.,!]*$", re.I)
MAX_SENTENCES = 8


class Conversation:
    def _pressure_hint(self) -> str:
        """Server-driven nudges so exam-tier items and real application questions actually happen."""
        import db
        out = ""
        cov = len([x for x in db.covered_sections(self.track, self.day) if x != "__l3__"])
        if self.mode in ("teach", "review") and (cov - self.l3_cov_mark >= 2 or (self.turns >= 10 and self.l3_asked == 0)):
            bank = prompts.content.load_day(self.track, self.day)
            qs = (bank or {}).get("questions") or []
            if qs and self.bank_idx < len(qs):
                out += (f"\n\n(System: it is time for ONE exam-tier (L3) item. Use Q{self.bank_idx + 1} from today's question bank: paraphrase the stem, read options A to D briefly, "
                        "and do not explain until he commits to an answer. Log it with level=\"3\".)")
                self.bank_idx += 1
            else:
                out += ("\n\n(System: it is time for ONE exam-tier (L3) item. Write an ORIGINAL full item: AWS = a 3 to 4 sentence business scenario with four options and a named distractor; "
                        "LSAT = a 4 to 6 sentence stimulus with a question stem and five answer choices; quant = an interview-style puzzle. Do not explain until he commits. Log it with level=\"3\".)")
            self.l3_cov_mark = cov; self.l3_asked += 0
        if self.since_graded >= 3:
            out += "\n\n(System: he has not answered a real question in 3 turns. This turn MUST end with a concrete application question he can answer, not a check-in.)"
        return out

    def day_done_refusal(self) -> str:
        """'' if day_done may be accepted, else a system hint explaining why not."""
        import progress
        left = progress.remaining(self.track, self.day)
        if left:
            return ("(System: day_done REFUSED. Not covered yet: " + "; ".join(f"[{x['id']}] {x['title']}" for x in left)
                    + ". Do NOT say goodbye. Teach the next one this very turn.)")
        if self.hits < 2:
            return ("(System: day_done REFUSED. He has not yet shown mastery today: fewer than 2 correct answers. Run a short exit ticket of 2 application questions "
                    "(one exam-style), then you may close the day.)")
        return ""

    def _last_session_fact(self) -> str:
        import db
        notes = db.recent_notes(self.track, 1)
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM sessions WHERE track=?", (self.track,)).fetchone()["n"]
        if n <= 1 and not notes:
            import content
            meta = content.TRACKS[self.track]
            first = "This is his first session in this app" + (f" but the track is at {meta['unit'].lower()} {self.day}, so do NOT call it day one." if self.day > 1 else ".")
            return f"Track: {meta['name']}, {meta['unit'].lower()} {self.day} of {meta['days']}. {first}"
        import content
        meta = content.TRACKS[self.track]
        return (f"Track: {meta['name']}, {meta['unit'].lower()} {self.day} of {meta['days']}. This is NOT the first session in this track, never say it is the first or 'day one'. "
                + (f"Last session debrief: {notes[-1]['note'][:300]}" if notes else ""))

    def __init__(self, track: str, day: int, mode: str):
        self.track, self.day, self.mode = track, day, mode
        self.system = prompts.system_prompt(track, day, mode)
        self.messages: list[dict] = []
        self.hint = ""  # one-shot system note for the next turn (e.g. a refused day_done)
        self.assent_run = 0
        self.turns = 0
        self.hits = 0          # graded hits this session (gate for day_done)
        self.fact_note = ""    # correction produced by the background fact-check of the previous mentor turn
        self.fact_task = None
        self.promised_board = False
        self.used_note = False
        self.l3_cov_mark = 0     # covered-section count at the last exam-tier item
        self.l3_asked = 0
        self.since_graded = 0
        self.bank_idx = 0

    def _payload(self, user_text: str | None):
        sysmsg = self.system + "\n\n" + prompts.mastery_block(self.track) + "\n\n" + prompts.coverage_block(self.track, self.day, self.turns) + "\n\n" + prompts.FINAL_REMINDERS
        msgs = [{"role": "system", "content": sysmsg}] + self.messages[-MAX_HISTORY:]
        if user_text is not None:
            hint = ""
            if self.fact_task is not None and self.fact_task.done():
                try:
                    self.fact_note = self.fact_task.result() or ""
                except Exception:  # noqa: BLE001
                    self.fact_note = ""
                self.fact_task = None
            self.used_note = bool(self.fact_note)
            if self.fact_note:
                hint += "\n\n(System: your previous turn contained an error: " + self.fact_note + " Correct it briefly and naturally at the start of this reply, then continue.)"; self.fact_note = ""
            if self.hint:
                hint += "\n\n" + self.hint; self.hint = ""
            hint += self._pressure_hint()
            if re.search(r"\b(draw|diagram|whiteboard|board|sketch|visuali[sz]e|illustrate|mermaid|picture)\b", user_text, re.I):
                hint += "\n\n(System note: he asked to see it. Emit a NEW <board> tag in this reply, with <layout board=\"wide\"/> first, then walk through it with <focus/> tags.)"
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
                self.hint = ("(System: his last " + str(self.assent_run) + " replies were only okay/sure/yes, which does not tell you whether it landed. "
                             "Do not advance. In a warm, light way, e.g. 'Let me make sure I explained that well, how would you put it in your own words?', ask him to say it back or predict one tiny case.)")
        parser, splitter = TagParser(), SentenceSplitter()
        spoken: list[str] = []
        truncated = False
        import progress
        grade_task = cover_task = None
        prev = next((m["content"] for m in reversed(self.messages) if m["role"] == "assistant"), "")
        if (user_text and not opener and not user_text.startswith("(") and prev.rstrip().endswith("?")
                and not ASSENT.match(user_text.strip()) and not user_text.strip().endswith("?") and not OFFER.search(prev[-160:])):
            import db
            known = [m["topic"] for m in db.mastery_map(self.track, 60)]
            grade_task = asyncio.create_task(grade_answer(prev, user_text, content_title(self.track, self.day), known))
        self.turns += 1
        if not opener and self.turns % 3 == 0 and self.turns >= 3:
            left = progress.remaining(self.track, self.day)
            if left:
                recent = "\n".join(("TUTOR: " if m["role"] == "assistant" else "STUDENT: ") + m["content"] for m in self.messages[-14:])
                cover_task = asyncio.create_task(judge_sections(left, recent))
        logged = False
        emitted = {"todo": False, "board": False}
        pending_l3: list = []
        if not opener and user_text and not user_text.startswith("("):
            self.since_graded += 1
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}

        def handle(events):
            for ev in events:
                if ev[0] == "log":
                    nonlocal logged
                    logged = True
                    if ev[2] == "hit":
                        self.hits += 1
                    self.since_graded = 0
                    if len(ev) > 3 and ev[3] and ev[3] >= 3:
                        self.l3_asked += 1; pending_l3.append(1)
                elif ev[0] in ("todo", "board"):
                    emitted[ev[0]] = True
                if ev[0] == "speech":
                    yield ("speech", ev[1])
                    for s in splitter.feed(ev[1]):
                        s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                        if s and not MACHINERY.search(s):
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
                        cap = 5 if opener else MAX_SENTENCES
                        if len(spoken) >= cap and (spoken[-1].endswith("?") or len(spoken) >= cap + 3):
                            truncated = not spoken[-1].endswith("?")
                            break
            for ev in handle(parser.flush()):
                yield ev
            for s in ([] if truncated else splitter.flush()):
                s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                if s:
                    spoken.append(s)
                    yield ("sentence", s)
            said = " ".join(spoken)
            if pending_l3:
                yield ("covered", "__l3__")
            if grade_task:
                g = await grade_task
                if g and not logged:
                    self.since_graded = 0
                    if g[1] == "hit":
                        self.hits += 1
                    if g[2] >= 3:
                        self.l3_asked += 1; yield ("covered", "__l3__")
                    elif spoken and PRAISE.search(spoken[0]):
                        self.fact_note = self.fact_note or "You praised an answer that was actually flawed or incomplete. Name the exact flaw now, kindly, before moving on."
                    yield ("log", g[0], g[1], g[2])
            if cover_task:
                for sid in await cover_task:
                    yield ("covered", sid)
            if HW_PROMISE.search(said) and not emitted["todo"] and not opener:
                t = await make_todo("\n".join(m["content"] for m in self.messages[-6:] if m["role"] == "assistant") + "\n" + said, content_title(self.track, self.day))
                if t:
                    yield ("todo", t[0], t[1])
            if BOARD_PROMISE.search(said) and not emitted["board"]:
                self.fact_note = (self.fact_note + " " if self.fact_note else "") + "You said you would show or draw something on the board but emitted no <board> tag. Emit the board now with the next sentence."
            if said and not opener and self.fact_task is None and not self.used_note:
                self.fact_task = asyncio.create_task(factcheck(prompts.content.lesson_text(self.track, self.day), said))
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


def cap_level(question: str, lvl: int) -> int:
    """Levels come from the question's structure, not the grader's generosity: L3 needs options or a full stimulus, L4 needs changed constraints or critique too."""
    opts = len(re.findall(r"(?:^|[\s(])[A-E][.):]\s", question))
    long_q = len(question.split()) >= 60
    structured = opts >= 3 or long_q
    if lvl >= 3 and not structured:
        lvl = 2
    if lvl >= 4 and not re.search(r"(what if|now suppose|suppose now|trade-?off|critique|changes? (one|the) constraint|flip)", question, re.I):
        lvl = 3
    return lvl


async def grade_answer(question: str, answer: str, lesson_title: str, topics: list[str]):
    """Strict independent grade of the student's reply to the mentor's last question. Returns (topic, result, level) or None."""
    known = "; ".join(topics[:40]) or "(none yet)"
    sysmsg = ("You grade a beginner's spoken answer (speech-to-text, so ignore small word errors) to a tutor's question. "
              "Reply ONLY JSON: {\"gradable\": true|false, \"topic\": \"...\", \"result\": \"hit\"|\"miss\", \"level\": 1-4}. "
              "gradable=false if the tutor's turn was an offer or check-in rather than a question needing knowledge or reasoning, if the student asked a question back, "
              "or if the reply is just okay/sure/I don't know. "
              "topic: choose EXACTLY one name from the known topics if it fits, otherwise a new 2-4 word name. "
              "level is the difficulty of the QUESTION: 1 recall/recognise, 2 apply to a new small case, 3 exam-style scenario with distractors, 4 professional transfer or design trade-off. "
              "If the stated verdict (yes/no, which option) contradicts the student's own stated reasoning, it is a miss. Be strict but fair: hit only if every key part of the answer is correct and the reasoning is not reversed; a clearly correct paraphrase is a hit; partial or reversed is miss. "
              f"Lesson context: {lesson_title}. Known topics: {known}")
    j = _json(await _quick(sysmsg, f"TUTOR TURN: {question[-700:]}\nSTUDENT ANSWER: {answer}"))
    if j and j.get("gradable") and j.get("topic") and j.get("result") in ("hit", "miss"):
        topic = str(j["topic"]).strip()
        for k in topics:
            if k.lower() == topic.lower():
                topic = k
        lvl = cap_level(question, j.get("level") if isinstance(j.get("level"), int) else 0)
        return topic, j["result"], lvl
    return None


async def factcheck(lesson: str, mentor_text: str) -> str:
    """Independent check of the mentor's turn. A lesson-based correction needs a verbatim quote from the lesson page that contradicts the claim;
    arithmetic errors need none. Returns a one-sentence correction or ''."""
    sysmsg = ("You fact-check a tutor's spoken turn against the LESSON PAGE and basic arithmetic. Flag ONLY clear errors: a wrong computed number, "
              "or a claim that a quoted line of the lesson page directly contradicts. Never flag something merely absent from the page, and never flag wording. "
              "Reply ONLY JSON: {\"kind\": \"arithmetic\"|\"lesson\"|\"none\", \"quote\": \"verbatim lesson line (for kind lesson)\", \"error\": \"one sentence: what was wrong and the correct fact\"}.")
    j = _json(await _quick(sysmsg, f"LESSON PAGE:\n{lesson[:14000]}\n\nTUTOR TURN:\n{mentor_text}", 200)) or {}
    kind, err = j.get("kind"), (j.get("error") or "").strip()
    if not err or kind not in ("arithmetic", "lesson"):
        return ""
    if kind == "lesson":
        norm = lambda t: re.sub(r"\W+", " ", t).lower().strip()
        q = norm(j.get("quote") or "")
        if len(q) < 20 or q not in norm(lesson):
            return ""
    return err


async def make_todo(recent: str, lesson_title: str):
    sysmsg = ("The tutor just promised the student a homework item but forgot to create it. From the transcript, write that homework. "
              "Reply ONLY JSON: {\"title\": \"short action title\", \"detail\": \"step by step instructions: exactly what to do, where, how long, how he knows it is done; steps separated by \\\\n\"}.")
    j = _json(await _quick(sysmsg, f"Lesson: {lesson_title}\nTranscript:\n{recent[-2500:]}", 400))
    if j and j.get("title"):
        return j["title"], str(j.get("detail", "")).replace("\\n", "\n")
    return None


async def judge_sections(remaining: list[dict], recent_mentor_text: str):
    """Which not-yet-covered sections has the mentor actually taught (substantively) in the recent turns? Returns ids."""
    if not remaining:
        return []
    listing = "\n".join(f"{x['id']}: {x['title']}" for x in remaining)
    sysmsg = ("You check which lesson sections a tutor has ACTUALLY TAUGHT in the transcript. A section counts only if its core idea was explained in substance "
              "(not merely mentioned or previewed) AND the student then engaged with it (answered a related question, predicted or explained something about it, even imperfectly). "
              "Reply ONLY JSON: {\"covered\": [ids]}. Use only ids from the list; empty list if none. When in doubt, leave it out.")
    j = _json(await _quick(sysmsg, f"SECTIONS NOT YET COVERED:\n{listing}\n\nTUTOR TRANSCRIPT:\n{recent_mentor_text[-3500:]}", 120))
    ids = {x["id"] for x in remaining}
    return [i for i in (j or {}).get("covered", []) if i in ids]
