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


def safe_calc(expr: str) -> str:
    """Evaluate a plain arithmetic expression (no names except a few math functions); returns spoken text."""
    import ast, math, operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
    fns = {"ln": math.log, "log": math.log, "exp": math.exp, "sqrt": math.sqrt, "round": round, "abs": abs}

    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)): return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in ops: return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in ops: return ops[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in fns and not n.keywords: return fns[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("unsupported")
    try:
        v = ev(ast.parse(expr.strip().replace("^", "**").replace("%", "/100"), mode="eval"))
        if v < 0:
            return "negative " + safe_calc(str(-v))
        if abs(v) >= 1000 or float(v).is_integer():
            return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"
        return f"{v:.4g}"
    except Exception:  # noqa: BLE001
        return "that number"


class TagParser:
    """Splits a token stream into speech text and hidden tags (board, log, day_done, note)."""

    PAIRED = ("board", "note", "todo", "calc")
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
            m = re.match(r"<(board|note|todo|calc|log|day_done|focus|layout|covered)\b", self.buf)
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
                elif name == "calc":
                    yield ("speech", safe_calc(body))
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


def db_has_todo(track: str, day: int) -> bool:
    import db
    return db.has_todo_for_day(track, day)


def _shingles(text: str, n: int = 6) -> set:
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def leaks(sentence: str, shingles: set) -> bool:
    return bool(shingles) and bool(_shingles(sentence) & shingles)


def content_title(track: str, day: int) -> str:
    import content
    d = content.load_day(track, day)
    return d["title"] if d else ""


OFFER = re.compile(r"(want (me )?to|want an|ready\?|shall we|should (we|i)|make sense|sound (good|doable|fair)|does that (connect|land|help)|any questions|better\?|clear\?)", re.I)
PRAISE = re.compile(r"^(exactly|perfect|precisely|that'?s (exactly )?right|correct|yes[,.!]|great|spot on|right[,.!])", re.I)
MACHINERY = re.compile(r"\b(I'?ll|I will|I'?m|I am|let me|I'?ve|I have|going to|gonna|now)\s+(just\s+)?(mark(ing|ed)?|log(ging|ged)?|record(ing|ed)?|sav(e|ing|ed))\b[^.?!]{0,40}\b(cover(ed)?|section|that|this|answer|correct(ly)?|progress|miss|hit|complete|done|day|today)\b|\bI('ve| have) covered\b|\bsection[^.?!]{0,30}\bcovered\b", re.I)
HW_STATEMENT = re.compile(r"\b(for homework|your homework|homework for (today|tonight)|here'?s your homework|your assignment (is|for))\b", re.I)
HW_PROMISE = re.compile(r"\b(your homework (is|for)|homework for (today|tonight)|for homework|your assignment (is|for)|I'?ll|I will|I'?ve|I have|let me|I'?m going to|going to)\b[^.?!]{0,40}\b(add|put|added|assign|assigned|give|save|drop)\b[^.?!]{0,50}\b(list|to-?do|homework|assignment)", re.I)
BOARD_PROMISE = re.compile(r"(look at the board|(see|on|check|at) the board|let me draw|i'?ll draw|i'?ll sketch|here'?s the (comparison|diagram|table)|the board shows)", re.I)

STOP_SIGNAL = re.compile(r"\b(that'?s (all|it|enough)|i('?m| am) (done|tired|out)|let'?s (stop|wrap|call it)|wrap (it )?up|end (the )?(session|class)|(today|day) (is|has been) (done|complete|over)|i have to go|gotta go|mark (today|it|this) (as )?(complete|done))\b", re.I)
GRADE_SWAPS = [(re.compile(r"\b(that'?s|that was|it'?s|this is) a (clean |solid )?hit\b", re.I), "that's right"),
               (re.compile(r"\b(that'?s|that was|it'?s|this is) a (clean )?miss\b", re.I), "that's not quite it"),
               (re.compile(r"\b(a )?(clean |solid )?hit\b(?= on| for)", re.I), "right"), (re.compile(r"\b(logged|graded) (as )?(a )?(hit|miss)\b", re.I), "noted")]
GRADE_WORDS = re.compile(r"\b(that'?s|that was|it'?s|a|clean|logged as|graded as|call that|counts as)\s+(a\s+)?(clean\s+)?(hit|miss)\b[.,!]?\s*", re.I)
NAME = re.compile(r",?\s*\bStanley\b(?=[,.!?]|\s)[,.]?", re.I)
PURE_Q = re.compile(r"^(so )?(what|why|how|can|could|does|do|is|are|should|would|when|where|which|who)\b[^.]{0,120}\?$", re.I)
COMPREHENSION = re.compile(r"(make sense|does that (land|click|connect|help)|sound (good|right|fair)|got it\?|clear so far|follow(ing)? so far|ok(ay)?\?$)", re.I)
ERRATA_TRAPS = [
    (re.compile(r"\b(creates?|provisions?|sets? up|builds?)\b[^.]{0,50}\bthree\b[^.]{0,30}\baccounts\b", re.I),
     "You said Control Tower creates three accounts. Correct: the management account already exists; Control Tower creates the log archive and audit accounts in the Security OU."),
    (re.compile(r"\btwo (enforcement |guardrail |control )?flavou?rs\b", re.I),
     "You said controls come in two flavors. Correct: three behaviors, preventive (SCPs), detective (Config rules) and proactive (CloudFormation Hooks)."),
    (re.compile(r"caller'?s SCPs? appl(y|ies),? not the target", re.I),
     "You said the caller's SCPs apply, not the target's. Correct: an SCP constrains the principals of the account it applies to; an assumed role is a principal of the role's account, so the target account's SCPs apply to it."),
    (re.compile(r"\b(30|thirty)[- ]plus (question )?types\b", re.I),
     "Do not state a count of LSAT question types; just name the types you are teaching."),
]
PURE_Q_SO = re.compile(r"^so (if|would|does|is|do|are|should|could|can)\b[^.]{0,200}\?$", re.I)
ASSENT = re.compile(r"^(ok(ay)?|sure|yes|yeah|yep|yup|right|got it|go on|continue|sounds good|makes sense|alright|uh[- ]huh|mm+ ?h?m*)[\s.,!]*(sir|mark)?[\s.,!]*$", re.I)
MAX_SENTENCES = 6


class Conversation:
    def stored_possible(self) -> bool:
        import progress
        return any(progress.item_for(self.track, self.day, x["id"]) for x in progress.sections_of(self.track, self.day))

    def _verdict_note(self, verdict) -> str:
        warm = " This was a warm-up question: if it was a miss, re-teach in two sentences and move on; never open by telling him he missed." if self.turns <= len(self.warm_targets) + 1 else ""
        return (f"(Grader verdict, already decided, you must not contradict it: {verdict[1].upper()} on '{verdict[0]}'. "
                + (f"Flaw: {verdict[3]} " if verdict[1] == "miss" and verdict[3] else "")
                + "Say it in plain words in your first two sentences (never the words hit, miss or graded). On a hit give one specific reason in at most 8 words. "
                  "Quote his words only if it was a miss; never invent an error he did not make. Do NOT emit a <log> tag." + warm + ")")

    def _gate_cover(self, sid: str) -> bool:
        """Single choke point for marking a section covered: needs HIT evidence on at least half its skills, one section per turn."""
        import progress as _pr
        if sid == "__l3__":
            return True
        if self.cover_flag:
            return False
        ids = {x["id"] for x in _pr.sections_of(self.track, self.day)}
        if ":" in sid or sid not in ids:
            return ":" in sid   # carried-over sections from earlier days pass through
        meta = _pr.section_meta(self.track, self.day).get(sid, {})
        sk = [x.lower() for x in meta.get("skills", [])]
        if meta.get("kind", "teach") == "teach" and sk:
            need = 1
            if sum(1 for x in sk if x in self.hit_skills) < need:
                self.dbg.append(("cover-blocked", sid)); return False
        self.cover_flag = True
        return True

    def _record(self, topic: str, result: str, level: int):
        """Update session counters for one graded answer; yields covered events (hit-backed, at most one section per turn)."""
        self.since_graded = 0
        self.graded_total += 1
        self.recent.append(result)
        self.recent = self.recent[-3:]
        if result == "hit":
            self.hits += 1
            self.hit_skills.add(topic.lower())
        self.skill_streak = (topic, (self.skill_streak[1] + 1) if (self.skill_streak[0] == topic and result == "hit") else (1 if result == "hit" else 0))
        self.graded_skills.add(topic.lower())
        if level >= 3:
            self.l3_asked += 1
            yield ("covered", "__l3__")
        if result == "hit" and level >= 2 or result == "hit" and self.mode != "teach":
            import progress as _pr
            meta = _pr.section_meta(self.track, self.day)
            done_now = prompts.db.covered_sections(self.track, self.day)
            for sec in _pr.sections_of(self.track, self.day):
                sk = [x.lower() for x in meta.get(sec["id"], {}).get("skills", [])]
                if sk and sec["id"] not in done_now and meta[sec["id"]].get("kind") == "teach" and self._gate_cover(sec["id"]):
                    yield ("covered", sec["id"])
                    break

    def _l3_due(self) -> bool:
        import db
        if self.mode not in ("teach", "review") or self.closing or self.warm_idx < len(self.warm_targets):
            return False
        cov = len([x for x in db.covered_sections(self.track, self.day) if x != "__l3__"])
        return cov - self.l3_cov_mark >= 2 or (self.turns >= 10 and self.l3_asked == 0 and self.item_tries == 0)

    def _start_item(self):
        """Pick a stored item for a covered section whose skills he has applied at L2; returns the board dict (server owns the item board)."""
        import db, progress
        covered = db.covered_sections(self.track, self.day)
        mm = {m["topic"].lower(): m["level"] for m in db.mastery_map(self.track, 200)}
        self.item_tries += 1
        for sec in reversed(progress.sections_of(self.track, self.day)):
            it = progress.item_for(self.track, self.day, sec["id"])
            if not it or sec["id"] in self.used_items:
                continue
            if str(it["answer"]).strip()[:1].upper() in "ABCDE" and len(str(it["answer"]).strip()) == 1 and not (it.get("options") and len(it["options"]) >= 4):
                continue  # malformed MCQ: options missing
            skills = progress.section_meta(self.track, self.day).get(sec["id"], {}).get("skills", [])
            lvl = max((mm.get(k.lower(), 0) for k in skills), default=0)
            force = len([x for x in covered if x != "__l3__"]) - self.l3_cov_mark >= 2
            if sec["id"] not in covered and lvl < 2 and not force:
                continue
            if skills and lvl < 2 and not force:
                self.hint += ("\n\n(System: before any exam-style item, give a FADED WORKED EXAMPLE of '" + skills[0] + "': you do the first half of a tiny case aloud, he finishes it.)")
                self.l3_cov_mark = len(covered)
                return None
            self.used_items.add(sec["id"]); self.l3_cov_mark = len(covered)
            opts = it.get("options") or {}
            body = it["stem"].strip() + ("\n\n" + "\n".join(f"- {k}. {v}" for k, v in opts.items()) if opts else "")
            self.item = {"sid": sec["id"], "it": it, "skill": (skills or [sec["title"]])[0]}
            self.hint += ("\n\n(System: a practice item is now on his whiteboard (stimulus and options). Do NOT emit a board and do NOT read or restate the item. "
                          "In at most 2 sentences tell him to read it on the board and give you his pick with a one-line reason. Reveal nothing.)")
            return {"title": "Practice item", "kind": "points", "body": body}
        self.l3_cov_mark = len(covered)
        return None

    def _resolve_item(self, text: str):
        """Grade an answer to the active stored item in code. Returns (verdict-or-None, note-or-None)."""
        it, skill = self.item["it"], self.item["skill"]
        opts = it.get("options") or {}
        t = text.strip()
        if re.search(r"\b(skip|pass|no idea|don'?t know|do not know|not sure|give up)\b", t, re.I) and not re.search(r"\b[A-E]\b", t):
            self.item = None
            return None, ("(He passed on the practice item. Walk through it in 3 short sentences: reveal the answer " + str(it["answer"]) + " and the reasoning: " + it.get("why", "") + ")")
        if opts:
            m = (re.search(r"\b(?:option|answer|choice|pick|go with|it'?s|is|choose)\s+([a-e])\b", t, re.I)
                 or re.search(r"(?<![A-Za-z'])([A-E])(?![A-Za-z'])", t) or (re.fullmatch(r"\W*([a-eA-E])\W*", t)))
            if not m:
                return None, "(He is still working on the practice item on the board. Help him think with a question or a nudge, WITHOUT revealing the answer or traps, and do not start a new item.)"
            pick = m.group(1).upper(); right = str(it["answer"]).strip().upper()[:1]
            self.item = None
            traps = it.get("traps", {})
            if pick == right:
                return (skill, "hit", 3, ""), (f"(Item result, decided in code: he chose {pick}, which is CORRECT. Say so in one short sentence with the specific reason: {it.get('why','')} "
                                               f"Then ask ONE probe: 'why not <a tempting wrong option>?' using these traps: {json.dumps(traps)}. Plain words, never hit or miss.)")
            return (skill, "miss", 3, f"chose {pick}"), (f"(Item result, decided in code: he chose {pick}, which is WRONG; the correct answer is {right}. The trap he fell into: {traps.get(pick, 'a tempting near miss')}. "
                                                         f"In two kind sentences explain why {pick} tempts and why {right} is right: {it.get('why','')} Then ask one short easier follow-up about the key constraint. Plain words, never hit or miss.)")
        self.item_last_skill = skill
        self.item = None   # open-answer item (quant): grade with the item key as reference
        return "OPEN", (it["stem"], f"Correct answer: {it['answer']}. Why: {it.get('why','')} Common wrong answers and traps: {json.dumps(it.get('traps', {}))}")

    def _pressure_hint(self) -> str:
        """Server-driven nudges so exam-tier items and real application questions actually happen."""
        import db, progress
        out = ""
        if not self.closing and not progress.remaining(self.track, self.day):
            if self.complete_base is None:
                self.complete_base = self.graded_total
            elif self.graded_total > self.complete_base:
                self.closing = True
        if self.closing:
            out += "\n\n(System: he wants to stop. No new questions or material. Give a 2 sentence recap, emit the <todo> homework if none yet, a hook for tomorrow, then <note> and <day_done/>.)"
            return out
        if self.warm_idx < len(self.warm_targets):
            t = self.warm_targets[self.warm_idx]; self.warm_idx += 1
            return out + (f"\n\n(System: WARM-UP. Before any new material, ask ONE quick retrieval question on his earlier skill '{t['topic']}' at rung L{max(2, min(4, t['level'] + 1))}"
                          f"{' (he missed it last time, so keep it gentle)' if t['last_result'] == 'miss' else ''}. Do not teach new material this turn.)")
        cov = len([x for x in db.covered_sections(self.track, self.day) if x != "__l3__"])
        if cov != self.last_cov:
            self.last_cov, self.turns_since_cov = cov, 0
        else:
            self.turns_since_cov += 1
        self.fallback_cd = max(0, self.fallback_cd - 1)
        if (self.mode in ("teach", "review") and self.fallback_cd == 0 and not self.item and not self.stored_possible()
                and (cov - self.l3_cov_mark >= 2 or (self.turns >= 12 and self.l3_asked == 0))):
            bank = prompts.content.load_day(self.track, self.day)
            qs = (bank or {}).get("questions") or []
            if qs and self.bank_idx < len(qs):
                out += (f"\n\n(System: it is time for ONE exam-tier (L3) item. Use Q{self.bank_idx + 1} from today's question bank: paraphrase the stem, read options A to D briefly, "
                        "and do not explain until he commits to an answer.)")
                self.bank_idx += 1
            else:
                out += ("\n\n(System: it is time for ONE exam-tier (L3) item. Write an ORIGINAL full item on a skill he has NOT drilled yet today: AWS = a 3 to 4 sentence business scenario with four options "
                        "(put them on the board with a <board kind=\"points\">) and a named distractor; LSAT = a 4 to 6 sentence stimulus, a question stem and five options on the board; quant = a short interview-style "
                        "question about a pitfall or concept (look-ahead bias, Sharpe, survivorship), NOT more return arithmetic. Do not explain until he commits.)")
            self.l3_cov_mark = cov
            self.fallback_cd = 6
        if self.item:
            it = self.item["it"]
            out += ("\n\n(System: the practice item currently on his board, keep everything you say consistent with it and do not post another board until it is resolved. "
                    f"STEM: {it['stem']} OPTIONS: {json.dumps(it.get('options') or {})} KEY: {it['answer']}. Do not reveal the key or traps until he commits to a letter.)")
        left = progress.remaining(self.track, self.day)
        struggling = sum(1 for r in self.recent if r != "hit") >= 2
        if left and self.turns_since_cov >= 5 and self.turns >= 6 and not struggling:
            out += (f"\n\n(System: you have spent about {self.turns_since_cov} turns without finishing a section. Move on NOW: teach the core idea of [{left[0]['id']}] {left[0]['title']} in this turn, "
                    "compactly, then one application question.)")
        if self.skill_streak[1] >= 2:
            out += f"\n\n(System: he has answered '{self.skill_streak[0]}' correctly {self.skill_streak[1]} times. Do NOT ask about that skill again; advance to the next skill or section.)"
        if self.since_graded >= 3:
            out += "\n\n(System: he has not answered a real question in 3 turns. This turn MUST end with a concrete application question he can answer, not a check-in.)"
        return out

    def day_done_refusal(self) -> str:
        """'' if day_done may be accepted, else a system hint explaining why not."""
        import progress
        left = progress.remaining(self.track, self.day)
        if self.closing:
            return ""  # he asked to stop: the day completes and uncovered sections carry forward automatically
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
        self.verdict_note = ""
        self.used_items = set()
        self.item = None
        self.name_turn = -10
        self.leak_shingles = set()
        self.recent = []
        self.cover_flag = False
        self.hit_skills = set()
        self.dbg = []
        self.item_last_skill = None
        self.fallback_cd = 0
        self.last_cov = 0
        self.turns_since_cov = 0
        self.skill_streak = (None, 0)
        self.graded_skills = set()
        self.graded_total = 0
        self.complete_base = None
        self.item_tries = 0
        self.closing = False
        self.warm_idx = 0
        import db
        import progress
        self.warm_targets = [t for t in db.due_topics(track, 12) if progress.review_worthy(track, t["topic"])][:2] if mode in ("teach", "review") else []
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
            if self.verdict_note:
                hint += "\n\n" + self.verdict_note; self.verdict_note = ""
            if self.hint:
                hint += "\n\n" + self.hint; self.hint = ""
            hint += self._pressure_hint()
            if re.search(r"\b(draw|diagram|whiteboard|board|sketch|visuali[sz]e|illustrate|mermaid|picture)\b", user_text, re.I):
                hint += "\n\n(System note: he asked to see it. Emit a NEW <board> tag in this reply, with <layout board=\"wide\"/> first, then walk through it with <focus/> tags.)"
            msgs.append({"role": "user", "content": user_text})
            if hint.strip():
                msgs.append({"role": "system", "content": "HIDDEN NOTES FOR THIS REPLY ONLY. Follow them silently. Never speak, quote or paraphrase the notes themselves:" + hint})
            self.leak_shingles = _shingles(hint + " " + prompts.FINAL_REMINDERS)
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
            first_due = ""
            if self.warm_targets:
                t = self.warm_targets[0]; self.warm_idx = 1
                first_due = f" Your very first question after the greeting is a warm-up retrieval on his earlier skill '{t['topic']}' (rung L{max(2, min(4, t['level'] + 1))}); do not start new material yet."
            user_text = "(Stanley just opened the app and is ready. Begin the session now, following the session shape. " + self._last_session_fact() + first_due + ")"
        elif user_text is not None and not user_text.startswith("("):
            if STOP_SIGNAL.search(user_text):
                self.closing = True
            self.assent_run = self.assent_run + 1 if ASSENT.match(user_text.strip()) else 0
            if self.assent_run >= 2:
                self.hint = ("(System: his last " + str(self.assent_run) + " replies were only okay/sure/yes, which does not tell you whether it landed. "
                             "Do not advance. In a warm, light way, e.g. 'Let me make sure I explained that well, how would you put it in your own words?', ask him to say it back or predict one tiny case.)")
        parser, splitter = TagParser(), SentenceSplitter()
        spoken: list[str] = []
        truncated = False
        import progress
        cover_task = None
        prev = next((m["content"] for m in reversed(self.messages) if m["role"] == "assistant"), "")
        verdict = None
        item_board = None
        self.cover_flag = False
        self.unsure_v = None
        answer_turn = False
        real_answer = user_text and not opener and not user_text.startswith("(")
        if real_answer and self.item:
            verdict, item_note = self._resolve_item(user_text)
            if verdict == "OPEN":
                import db, progress as _pg
                known = _pg.skills_for_day(self.track, self.day) + [m["topic"] for m in db.mastery_map(self.track, 40)]
                verdict = await grade_answer(item_note[0], user_text, content_title(self.track, self.day), known, item_note[1], think=(self.track == "quant"))
                item_note = None
                if verdict and verdict[1] != "unsure":
                    verdict = (self.item_last_skill or verdict[0], verdict[1], 3, verdict[3]); self.verdict_note = self._verdict_note(verdict)
                else:
                    verdict = None; self.verdict_note = "(Grader could not settle the answer to the practice item. Ask ONE short probing follow-up; do not rule yet.)"
            elif item_note:
                self.verdict_note = item_note
        elif real_answer and prev.rstrip().endswith("?") and ASSENT.match(user_text.strip()) and COMPREHENSION.search(prev[-120:]):
            self.verdict_note = ("(He gave a bare yes to a comprehension check, which tells you nothing. Do not move on. Ask ONE concrete prediction or apply-it question about the idea you just taught.)")
        elif real_answer and prev.rstrip().endswith("?") and ASSENT.match(user_text.strip()) and not OFFER.search(prev[-160:]):
            self.verdict_note = "(He only said okay or sure and did not answer your question. Do not rule on anything. Re-ask it more simply, or give a smaller first step.)"
        elif (real_answer and prev.rstrip().endswith("?")
                and not ASSENT.match(user_text.strip()) and not PURE_Q.match(user_text.strip()) and not PURE_Q_SO.match(user_text.strip()) and not OFFER.search(prev[-160:])):
            answer_turn = True
            import db, progress as _pg
            known = _pg.skills_for_day(self.track, self.day) + [m["topic"] for m in db.mastery_map(self.track, 40)]
            lesson_ref = prompts.ERRATA + "\n" + prompts.content.lesson_text(self.track, self.day)
            verdict = await grade_answer(prev, user_text, content_title(self.track, self.day), known, lesson_ref, think=(self.track == "quant"))
            if verdict and verdict[1] == "unsure":
                self.unsure_v = verdict
                self.verdict_note = ("(Grader could not settle this answer. Judge it yourself from the lesson and be honest: if any part is wrong or vague, say which part kindly; "
                                     "if it is right, say so briefly. Emit your own <log> tag for it with level.)")
                verdict = None
            elif verdict:
                self.verdict_note = self._verdict_note(verdict)
        if real_answer and not self.item and self._l3_due():
            item_board = self._start_item()
        self.turns += 1
        if not opener and self.turns % 3 == 0 and self.turns >= 3:
            left = progress.remaining(self.track, self.day)
            if left:
                recent = "\n".join(("TUTOR: " if m["role"] == "assistant" else "STUDENT: ") + m["content"] for m in self.messages[-14:])
                cover_task = asyncio.create_task(judge_sections(left, recent))
        logged = False
        mentor_logged: list = []
        import progress as _pgm
        emitted = {"todo": False, "board": False}
        if not opener and user_text and not user_text.startswith("("):
            self.since_graded += 1
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}

        def handle(events):
            for ev in events:
                if ev[0] == "log":
                    if verdict or not answer_turn or mentor_logged:
                        continue  # a decisive grader verdict (or a non-answer turn) is the single source of truth
                    mentor_logged.append(1)
                    lvl = cap_level(prev, ev[3] if len(ev) > 3 else 0)
                    known = {k.lower(): k for k in (_pgm.skills_for_day(self.track, self.day))}
                    topic = known.get(ev[1].lower(), ev[1])
                    for e2 in self._record(topic, "hit" if ev[2] == "hit" else "miss", lvl):
                        yield e2
                    yield ("log", topic, "hit" if ev[2] == "hit" else "miss", lvl, "mentor")
                    continue
                if ev[0] == "covered":
                    if self._gate_cover(ev[1]):
                        yield ev
                    continue
                if ev[0] == "board" and (self.item or item_board):
                    self.dbg.append(("board-dropped", ev[1].get("title", "")))
                    continue
                if ev[0] in ("todo", "board"):
                    emitted[ev[0]] = True
                if ev[0] == "speech":
                    yield ("speech", ev[1])
                    for s in splitter.feed(ev[1]):
                        s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                        for rx, rep in GRADE_SWAPS:
                            s = rx.sub(rep, s)
                        if self.turns - self.name_turn < 5:
                            s = NAME.sub("", s).strip()
                        elif NAME.search(s):
                            self.name_turn = self.turns
                        trap = next((fix for rx, fix in ERRATA_TRAPS if rx.search(s)), None)
                        if trap:
                            self.dbg.append(("errata-trap", s)); self.fact_note = trap; s = ""
                        if s and re.fullmatch(r"(so )?(does that |do you )?(make sense|follow|get it|see (that|what i mean))( so far)?\??|(sound|does that sound) (good|right|fair)\??|(is that )?(clear|ok(ay)?)( so far)?\??", s.strip(" ."), re.I):
                            s = ""
                        if s and (MACHINERY.search(s) or (len(s.split()) >= 8 and leaks(s, self.leak_shingles))):
                            self.dbg.append(("stripped", s)); s = ""
                        if s:
                            spoken.append(s)
                            yield ("sentence", s)
                else:
                    yield ev

        try:
            if verdict:
                for ev in self._record(verdict[0], verdict[1], verdict[2]):
                    yield ev
                yield ("log", verdict[0], verdict[1], verdict[2], verdict[4] if len(verdict) > 4 else "")
            if item_board:
                yield ("board", item_board)
                emitted["board"] = True
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
                        if len(spoken) >= cap and (spoken[-1].endswith("?") or len(spoken) >= cap + 1) and not (spoken[-1].count('"') % 2 == 1 and len(spoken) < cap + 4):
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
            if self.unsure_v and not mentor_logged:
                self.since_graded = 0
                yield ("log", self.unsure_v[0], "partial", min(2, self.unsure_v[2]), "unsure")
            if cover_task:
                for sid in await cover_task:
                    if self._gate_cover(sid):
                        yield ("covered", sid)
            if (HW_PROMISE.search(said) or HW_STATEMENT.search(said)) and not emitted["todo"] and not opener and not db_has_todo(self.track, self.day):
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


async def _quick(system: str, user: str, max_tokens: int = 160, think: bool = False) -> str:
    body = {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "thinking": {"type": "enabled"} if think else {"type": "disabled"}, "temperature": 0, "max_tokens": max_tokens + (1500 if think else 0)}
    try:
        async with httpx.AsyncClient(timeout=90 if think else 30) as client:
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


async def grade_answer(question: str, answer: str, lesson_title: str, topics: list[str], reference: str = "", think: bool = False):
    """Strict independent grade of the student's reply to the mentor's last question. Returns (topic, result, level) or None."""
    known = "; ".join(topics[:40]) or "(none yet)"
    sysmsg = ("You grade a beginner's spoken answer (speech-to-text, so ignore small word errors) to a tutor's question. "
              "Reply ONLY JSON: {\"gradable\": true|false, \"topic\": \"...\", \"result\": \"hit\"|\"miss\"|\"unsure\", \"level\": 1-4, \"ref_quote\": \"if miss: the verbatim REFERENCE line that his answer contradicts\", \"flaw\": \"if miss: the exact words from the student's answer that were wrong or missing, quoted, plus the correct fact in one sentence; else empty\"}. "
              "gradable=false if the tutor's turn was an offer or check-in rather than a question needing knowledge or reasoning, if the student asked a question back, "
              "or if the reply is just okay/sure/I don't know. "
              "topic: choose EXACTLY one name from the known topics if it fits, otherwise a new 2-4 word name. "
              "level is the difficulty of the QUESTION: 1 recall/recognise, 2 apply to a new small case, 3 exam-style scenario with distractors, 4 professional transfer or design trade-off. "
              "If the stated verdict (yes/no, which option) contradicts the student's own stated reasoning, it is a miss. Be strict but fair: hit only if every key part of the answer is correct and the reasoning is not reversed; a clearly correct paraphrase is a hit; partial or reversed is miss. "
              f"Lesson context: {lesson_title}. Known topics: {known}" + (f"\nREFERENCE (ground truth; trust it over your own recall): {reference[:9000]}" if reference else ""))
    j = _json(await _quick(sysmsg, f"TUTOR TURN: {question[-700:]}\nSTUDENT ANSWER: {answer}", 200, think=think))
    if j and j.get("gradable") and j.get("topic") and j.get("result") in ("hit", "miss", "unsure"):
        if j["result"] == "miss":
            norm = lambda t: re.sub(r"\W+", " ", str(t)).lower().strip()
            rq = norm(j.get("ref_quote") or "")
            if len(str(j.get("flaw") or "")) < 12 or not reference or (len(rq) >= 15 and rq not in norm(reference)) or (len(rq) < 15 and think is False and False):
                j["result"] = "unsure"
        topic = str(j["topic"]).strip()
        for k in topics:
            if k.lower() == topic.lower():
                topic = k
        lvl = cap_level(question, j.get("level") if isinstance(j.get("level"), int) else 0)
        if j["result"] == "miss":
            chk = await _quick("Answer only yes or no. Does the STUDENT ANSWER clearly contradict or fail the REFERENCE LINE? Ignore wording differences; if the student's meaning is compatible with the line, answer no.",
                               f"REFERENCE LINE: {j.get('ref_quote')}\nSTUDENT ANSWER: {answer}\nTUTOR QUESTION: {question[-400:]}", 5)
            if not chk.lower().startswith("yes"):
                j["result"] = "unsure"
        return topic, j["result"], lvl, str(j.get("flaw") or "")[:300], json.dumps({k: j.get(k) for k in ("result", "ref_quote", "flaw")})[:300]
    return None


async def factcheck(lesson: str, mentor_text: str) -> str:
    """Independent check of the mentor's turn. A lesson-based correction needs a verbatim quote from the lesson page that contradicts the claim;
    arithmetic errors need none. Returns a one-sentence correction or ''."""
    sysmsg = ("You fact-check a tutor's spoken turn against the LESSON PAGE and basic arithmetic. Flag ONLY clear errors: a wrong computed number, "
              "or a claim that a quoted line of the lesson page directly contradicts. Never flag something merely absent from the page, and never flag wording. "
              "Reply ONLY JSON: {\"kind\": \"arithmetic\"|\"lesson\"|\"none\", \"quote\": \"verbatim lesson line (for kind lesson)\", \"error\": \"one sentence: what was wrong and the correct fact\"}.")
    j = _json(await _quick(sysmsg, f"LESSON PAGE:\n{prompts.ERRATA}\n{lesson[:14000]}\n\nTUTOR TURN:\n{mentor_text}", 200)) or {}
    kind, err = j.get("kind"), (j.get("error") or "").strip()
    if not err or kind not in ("arithmetic", "lesson"):
        return ""
    if kind == "lesson":
        norm = lambda t: re.sub(r"\W+", " ", t).lower().strip()
        q = norm(j.get("quote") or "")
        if len(q) < 20 or q not in norm(prompts.ERRATA + lesson):
            return ""
    return err


async def make_todo(recent: str, lesson_title: str):
    sysmsg = ("The tutor just promised the student a homework item but forgot to create it. From the transcript, write that homework. Only refer to materials that actually exist in the lesson; otherwise define the task fully yourself. "
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
