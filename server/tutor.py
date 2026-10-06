"""Conversation with the tutor model: streaming, hidden-tag parsing, sentence splitting."""
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

    PAIRED = ("board", "note")
    SELF = ("log", "day_done", "focus", "layout")

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
            m = re.match(r"<(board|note|log|day_done|focus|layout)\b", self.buf)
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


class Conversation:
    def __init__(self, track: str, day: int, mode: str):
        self.track, self.day, self.mode = track, day, mode
        self.system = prompts.system_prompt(track, day, mode)
        self.messages: list[dict] = []

    def _payload(self, user_text: str | None):
        msgs = [{"role": "system", "content": self.system}] + self.messages[-MAX_HISTORY:]
        if user_text is not None:
            msgs.append({"role": "user", "content": user_text})
        return {
            "model": MODEL,
            "messages": msgs,
            "stream": True,
            "thinking": {"type": "disabled"},
            "temperature": 0.8,
            "max_tokens": 500,
        }

    async def respond(self, user_text: str | None, opener: bool = False):
        """Async generator of events: ('speech', str) | ('sentence', str) | ('board', dict) | ('log', topic, result)
        | ('day_done',) | ('note', str) | ('error', str). Whatever was emitted counts as said (kept in history),
        even if the caller stops early (barge-in)."""
        if opener:
            user_text = "(Stanley just opened the app and is ready. Begin the session now, following the session shape.)"
        parser, splitter = TagParser(), SentenceSplitter()
        spoken: list[str] = []
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}

        def handle(events):
            for ev in events:
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
                            delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                        except Exception:
                            continue
                        if delta:
                            for ev in handle(parser.feed(delta)):
                                yield ev
            for ev in handle(parser.flush()):
                yield ev
            for s in splitter.flush():
                s = re.sub(r"[*_`#]+", "", s).replace("\u2014", ", ").replace("\u2013", ", ").strip()
                if s:
                    spoken.append(s)
                    yield ("sentence", s)
        except httpx.HTTPError as e:
            yield ("error", f"Network problem talking to the model: {e}")
        finally:
            reply = " ".join(spoken).strip()
            if user_text is not None:
                self.messages.append({"role": "user", "content": user_text})
            if reply:
                self.messages.append({"role": "assistant", "content": reply})
