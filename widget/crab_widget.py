"""Study Mentor crab: a pixel crab that walks onto the screen at study time."""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QObject, QPoint, QProcess, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QApplication, QWidget

BASE = "http://127.0.0.1:8765"
HOME = Path.home()
SCHEDULE = HOME / ".config/study-mentor/schedule.json"
FIRED = HOME / ".local/state/study-mentor/fired.json"
RUN_SH = str(Path(__file__).resolve().parent.parent / "run.sh")
DEFAULT = {
    "reminders": [
        {"id": "aws", "time": "19:30", "days": [0, 1, 2, 3, 4], "track": "aws", "enabled": True},
        {"id": "lsat", "time": "20:30", "days": [0, 1, 2, 3, 4], "track": "lsat", "enabled": False},
        {"id": "quant", "time": "21:00", "days": [0, 1, 2, 3, 4], "track": "quant", "enabled": False},
    ],
    "snooze_min": 10,
}
SHORT = {"aws": "AWS Pro", "lsat": "LSAT", "quant": "Quant"}
LINES = {
    "aws": ["Hey Stanley, time for your {n} prep. {u} {d}, {t}. Ready?",
            "Knock knock, it is me, the crab. {n} is up: {u} {d}, {t}. Shall we?",
            "The cloud will not study itself. {u} {d} of {n}: {t}. Ready?"],
    "lsat": ["Hey Stanley, time for {n}. {u} {d}, {t}. Ready to argue with logic?",
             "Logic called, it wants a word. {n}, {u} {d}: {t}. Ready?",
             "Claws up, brain on. {n}, {u} {d}: {t}. Shall we?"],
    "quant": ["Hey Stanley, number time. {n}, {u} {d}: {t}. Ready?",
              "Crabs count in eights, you count in answers. {n}, {u} {d}: {t}. Ready?",
              "Let us make the numbers behave. {n}, {u} {d}: {t}. Shall we?"],
}
GENERIC = ["Hey Stanley, study time! The app is asleep, but I can wake it up. Ready?",
           "Psst, Stanley. Study time. Want me to open the app?"]

# crab sprite on a 16x12 grid: (x, y, w, h)
CLAW_L = [(2, 1, 2, 1), (1, 2, 4, 2), (2, 4, 2, 1)]
CLAW_R = [(12, 1, 2, 1), (11, 2, 4, 2), (12, 4, 2, 1)]
BODY = [(5, 4, 6, 1), (4, 5, 8, 1), (3, 6, 10, 2), (4, 9, 8, 1)]
SHADE = [(3, 8, 10, 1)]
LEGS = [[(3, 10, 1, 1), (5, 10, 1, 1), (10, 10, 1, 1), (12, 10, 1, 1), (2, 11, 1, 1), (13, 11, 1, 1)],
        [(4, 10, 1, 1), (6, 10, 1, 1), (9, 10, 1, 1), (11, 10, 1, 1), (3, 11, 1, 1), (12, 11, 1, 1)]]
EYES = [(5, 6), (10, 6)]
SCALE, CW, CH = 8, 16 * 8, 12 * 8
FLOOR_GAP = 110
BUBBLE_W, PAD = 420, 16


def http_get(path: str, timeout: float = 2.0) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_schedule() -> dict:
    if not SCHEDULE.exists():
        write_json(SCHEDULE, DEFAULT)
    return read_json(SCHEDULE, DEFAULT)


def build_message(track: str) -> tuple[str, bool]:
    """Return (spoken line, server_up)."""
    try:
        state = json.loads(http_get("/api/state"))
        t = next(x for x in state["tracks"] if x["id"] == track)
    except Exception:
        return random.choice(GENERIC), False
    line = random.choice(LINES.get(track, LINES["aws"]))
    return line.format(n=SHORT.get(track, t["name"]), u=t.get("unit", "Day"), d=t["day"], t=t["title"]), True


def server_up() -> bool:
    try:
        http_get("/api/state", 1.5)
        return True
    except Exception:
        return False


class Worker(QObject):
    """Runs blocking network work off the UI thread."""
    audio = Signal(str)
    launched = Signal()

    def fetch_tts(self, text: str) -> None:
        def run():
            path = ""
            try:
                q = urllib.parse.urlencode({"text": text, "voice_id": "bm_george", "speed": "1.0"})
                data = http_get("/api/tts?" + q, 60)
                fd, path = tempfile.mkstemp(suffix=".wav", prefix="crab-")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except Exception:
                path = ""
            self.audio.emit(path)
        threading.Thread(target=run, daemon=True).start()

    def launch_app(self) -> None:
        def run():
            if not server_up():
                r = subprocess.run(["systemctl", "--user", "start", "study-mentor.service"],
                                   capture_output=True)
                if r.returncode != 0:
                    subprocess.Popen([RUN_SH], start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(40):
                    if server_up():
                        break
                    threading.Event().wait(0.5)
            QProcess.startDetached("flatpak", [
                "run", "--socket=x11", "com.google.Chrome", "--ozone-platform=x11",
                f"--user-data-dir={HOME}/.var/app/com.google.Chrome/config/study-mentor-profile",
                f"--app={BASE}"])
            self.launched.emit()
        threading.Thread(target=run, daemon=True).start()


class Crab(QWidget):
    IDLE, WALK_IN, WAIT, WALK_OUT = range(4)

    def __init__(self, test_mode: bool, once: bool):
        super().__init__()
        self.test_mode, self.once = test_mode, once
        self.state = self.IDLE
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setWindowTitle("Study Mentor crab")
        self.worker = Worker()
        self.worker.audio.connect(self.on_audio)
        self.worker.launched.connect(self.walk_out)
        self.player = QProcess(self)
        self.player.finished.connect(self.on_played)
        self.wav = ""
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.tick)
        self.clock = QElapsedTimer()
        self.idle_timer = QTimer(self, singleShot=True, interval=180_000)
        self.idle_timer.timeout.connect(self.on_timeout)
        self.font_text = QFont("Inter")
        self.font_text.setPixelSize(15)
        self.font_btn = QFont("Inter")
        self.font_btn.setPixelSize(14)
        self.font_btn.setWeight(QFont.DemiBold)
        self.track, self.message, self.up = "aws", "", True
        self.can_refire = True
        self.hover = -1
        self.buttons: list[tuple[str, QRect]] = []
        self.last_mask = QRect()

    # ---- lifecycle ----
    def fire(self, track: str, refire_ok: bool = True) -> bool:
        if self.state != self.IDLE:
            return False
        self.track, self.can_refire = track, refire_ok
        self.message, self.up = build_message(track)
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry()
        self.winId()  # create the native window so it can be pinned to the primary screen
        self.windowHandle().setScreen(scr)
        self.setGeometry(g)
        self.show()
        self.floor = self.height() - FLOOR_GAP
        self.x = float(self.width() + 10)
        self.target = self.width() * 0.6
        self.t = 0.0
        self.speak_t = 0.0
        self.waving = False
        self.bubble_t = 0.0
        self.state = self.WALK_IN
        self.hover = -1
        self.worker.fetch_tts(self.message)
        self.clock.start()
        self.timer.start()
        return True

    def walk_out(self) -> None:
        self.stop_audio()
        self.waving = False
        self.idle_timer.stop()
        self.state = self.WALK_OUT

    def finish(self) -> None:
        self.timer.stop()
        self.hide()
        self.state = self.IDLE
        if self.test_mode or self.once:
            QApplication.quit()

    def snooze(self, auto: bool = False) -> None:
        if self.can_refire and not self.test_mode:
            mins = int(load_schedule().get("snooze_min", 10))
            track = self.track
            QTimer.singleShot(mins * 60_000, lambda: self.fire(track, refire_ok=not auto))
        self.walk_out()

    def on_timeout(self) -> None:
        if self.state == self.WAIT:
            self.snooze(auto=True)

    # ---- audio ----
    def on_audio(self, path: str) -> None:
        if not path:
            return
        if self.state == self.IDLE:
            os.unlink(path)
            return
        self.wav = path
        self.player.start("pw-play", [path])
        if not self.player.waitForStarted(800):
            self.player.start("paplay", [path])
        self.waving = True

    def on_played(self) -> None:
        self.waving = False
        if self.wav:
            try:
                os.unlink(self.wav)
            except OSError:
                pass
            self.wav = ""

    def stop_audio(self) -> None:
        if self.player.state() != QProcess.NotRunning:
            self.player.kill()
        self.on_played()

    # ---- animation ----
    def tick(self) -> None:
        dt = min(self.clock.restart() / 1000.0, 0.1)
        self.t += dt
        speed = 230.0
        if self.state == self.WALK_IN:
            self.x -= speed * dt
            if self.x <= self.target:
                self.x = self.target
                self.state = self.WAIT
                self.idle_timer.start()
        elif self.state == self.WALK_OUT:
            self.x += speed * dt
            self.bubble_t = max(0.0, self.bubble_t - dt * 5)
            if self.x > self.width() + 20:
                self.finish()
                return
        if self.state == self.WAIT:
            self.bubble_t = min(1.0, self.bubble_t + dt / 0.3)
        self.update_mask()
        self.update()

    def bubble_rect(self) -> QRect:
        fm = QFontMetrics(self.font_text)
        inner = BUBBLE_W - 2 * PAD
        th = fm.boundingRect(QRect(0, 0, inner, 2000), Qt.TextWordWrap, self.message).height()
        h = PAD + th + 14 + 40 + PAD
        cx = int(self.x + CW / 2)
        x = max(12, min(cx - BUBBLE_W // 2, self.width() - BUBBLE_W - 12))
        y = int(self.floor - CH - 18 - h)
        return QRect(x, y, BUBBLE_W, h)

    def crab_rect(self) -> QRect:
        return QRect(int(self.x), int(self.floor - CH), CW, CH)

    def update_mask(self) -> None:
        r = self.crab_rect().adjusted(-6, -14, 6, 8)
        if self.bubble_t > 0:
            r = r.united(self.bubble_rect().adjusted(-4, -4, 4, 24))
        r = r.intersected(self.rect())
        if r != self.last_mask:
            self.last_mask = r
            self.setMask(QRegion(r))

    # ---- painting ----
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        self.draw_crab(p)
        if self.bubble_t > 0:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.TextAntialiasing, True)
            self.draw_bubble(p)

    def draw_crab(self, p: QPainter) -> None:
        moving = self.state in (self.WALK_IN, self.WALK_OUT)
        frame = int(self.t / 0.12) % 2 if moving else 0
        bob = 4 if moving and frame else 0
        wave = int(self.t / 0.22) % 2 if self.waving else 0
        ox, oy = int(self.x), int(self.floor - CH) - bob

        def rects(items, color, dy=0):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            for x, y, w, h in items:
                p.drawRect(ox + x * SCALE, oy + (y + dy) * SCALE, w * SCALE, h * SCALE)

        rects(BODY, "#8FA0F5")
        rects(SHADE, "#7083E8")
        rects(LEGS[frame], "#7083E8")
        rects(CLAW_L, "#B4C0FF", -1 if wave else 0)
        rects(CLAW_R, "#B4C0FF", 0 if wave else -1 if self.waving else 0)
        for ex, ey in EYES:
            p.setBrush(QColor("#0C1030"))
            p.drawRect(ox + ex * SCALE, oy + ey * SCALE, SCALE, SCALE)
            p.setBrush(QColor("#FFFFFF"))
            p.drawRect(ox + ex * SCALE + 4, oy + ey * SCALE, 3, 3)

    def draw_bubble(self, p: QPainter) -> None:
        t = self.bubble_t
        e = 1 - (1 - t) ** 3
        r = self.bubble_rect()
        anchor = QPoint(r.center().x(), r.bottom() + 14)
        p.save()
        p.setOpacity(e)
        p.translate(anchor)
        s = 0.9 + 0.1 * e
        p.scale(s, s)
        p.translate(-anchor)
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), 18, 18)
        tail_x = max(r.left() + 30, min(int(self.x + CW / 2), r.right() - 30))
        tail = QPainterPath()
        tail.moveTo(tail_x - 10, r.bottom())
        tail.lineTo(tail_x, r.bottom() + 12)
        tail.lineTo(tail_x + 10, r.bottom())
        tail.closeSubpath()
        shape = path.united(tail)
        p.setBrush(QColor("#12152A"))
        border = QColor("#8FA0F5")
        border.setAlphaF(0.6)
        p.setPen(QPen(border, 1))
        p.drawPath(shape)
        p.setPen(QColor("#ECEEF8"))
        p.setFont(self.font_text)
        inner = QRect(r.left() + PAD, r.top() + PAD, BUBBLE_W - 2 * PAD, r.height())
        p.drawText(inner, Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft, self.message)
        self.layout_buttons(r)
        p.setFont(self.font_btn)
        for i, (label, br) in enumerate(self.buttons):
            primary = i == 0
            hot = i == self.hover
            if primary:
                p.setBrush(QColor("#FDA85C" if hot else "#FB923C"))
                p.setPen(Qt.NoPen)
            else:
                p.setBrush(QColor("#232A44" if hot else "#1A1F33"))
                p.setPen(QPen(QColor("#38426A"), 1))
            p.drawRoundedRect(QRectF(br), 12, 12)
            p.setPen(QColor("#2B1203" if primary else "#ECEEF8"))
            p.drawText(br, Qt.AlignCenter, label)
        p.restore()

    def layout_buttons(self, r: QRect) -> None:
        labels = ["Let's go" if self.up else "Start the app", "10 more minutes", "Not today"]
        fm = QFontMetrics(self.font_btn)
        widths = [fm.horizontalAdvance(s) + 28 for s in labels]
        x, y = r.left() + PAD, r.bottom() - PAD - 40
        self.buttons = []
        for label, w in zip(labels, widths):
            self.buttons.append((label, QRect(x, y, w, 40)))
            x += w + 8

    # ---- input ----
    def button_at(self, pos: QPoint) -> int:
        if self.state != self.WAIT or self.bubble_t < 0.9:
            return -1
        for i, (_, r) in enumerate(self.buttons):
            if r.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, e) -> None:
        self.hover = self.button_at(e.position().toPoint())
        self.setCursor(Qt.PointingHandCursor if self.hover >= 0 else Qt.ArrowCursor)

    def mousePressEvent(self, e) -> None:
        i = self.button_at(e.position().toPoint())
        if i < 0:
            return
        self.idle_timer.stop()
        if i == 0:
            self.worker.launch_app()
            self.state = self.WAIT
            self.bubble_t = 1.0
            self.buttons = []
            self.message = "On it, opening the app..."
        elif i == 1:
            self.snooze()
        else:
            self.walk_out()


def due_reminders(fired: dict, now: datetime) -> list[dict]:
    today, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    out = []
    for r in load_schedule().get("reminders", []):
        if (r.get("enabled") and r.get("time") == hhmm and now.weekday() in r.get("days", [])
                and fired.get(r["id"]) != today):
            out.append(r)
    return out


def check(crab: Crab, fired: dict) -> bool:
    now = datetime.now()
    for r in due_reminders(fired, now):
        if crab.fire(r["track"]):
            fired[r["id"]] = now.strftime("%Y-%m-%d")
            write_json(FIRED, fired)
            return True
    return False


def main() -> int:
    args = sys.argv[1:]
    test = "--test" in args
    once = "--once" in args
    QGuiApplication.setDesktopFileName("study-mentor-crab")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    crab = Crab(test, once)
    fired = read_json(FIRED, {})
    if test:
        i = args.index("--test")
        track = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else "aws"
        QTimer.singleShot(100, lambda: crab.fire(track, refire_ok=False))
    elif once:
        QTimer.singleShot(0, lambda: None if check(crab, fired) else app.quit())
    else:
        poll = QTimer(interval=20_000)
        poll.timeout.connect(lambda: check(crab, fired))
        poll.start()
        QTimer.singleShot(0, lambda: check(crab, fired))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
