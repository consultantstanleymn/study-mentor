"""Multi-day chain sim: runs days N..N+K-1 in one throwaway DB with a simulated one-day clock advance between sessions.
Usage: sim_multi.py label track day persona [days=5]. Writes a transcript plus a metrics summary (levels, due, backlog, retention hits)."""
import asyncio, json, os, random, sys, time
import sim
from sim import db, tutor, progress, ROOT

async def run_day(track, d, persona, turns, key, label):
    # like sim.run but without re-marking earlier days done: the chain's own history stands
    conv = tutor.Conversation(track, d, "teach")
    P = sim.PERSONAS[persona]; rng = random.Random(label + str(d)); lines, hist, events = [], [], []
    text, tags = await sim.mentor_turn(conv, None, opener=True); lines.append(("MENTOR", text, tags)); hist.append(("m", text))
    for i in range(turns):
        beh = rng.choices(list(P["w"]), weights=list(P["w"].values()))[0]
        msgs = [{"role": "system", "content": sim.STUDENT_SYS.format(desc=P["desc"]) + f"\nThis turn: {sim.BEHAVIOR[beh]}"}]
        for who, t in hist[-12:]: msgs.append({"role": "user" if who == "m" else "assistant", "content": t})
        reply = await sim.chat(msgs, 90, 1.0, key)
        lines.append((f"STUDENT[{beh}]", reply, [])); hist.append(("s", reply))
        text, tags = await sim.mentor_turn(conv, reply); lines.append(("MENTOR", text, tags)); hist.append(("m", text))
        if any(t.startswith("day_done:OK") for t in tags): break
    return conv, lines

async def main():
    label, track, day, persona = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    days = int(sys.argv[5]) if len(sys.argv) > 5 else 5
    for d in range(1, day): db.mark_day(track, d, "done")
    key = tutor.api_key(); turns = int(os.environ.get("SIM_TURNS", "24"))
    out, summary = [f"# {label} chain {track} days {day}-{day+days-1} persona={persona}\n"], []
    for d in range(day, day + days):
        conv, lines = await run_day(track, d, persona, turns, key, label)
        out.append(f"\n## DAY {d}\n")
        warm_hits = warm_n = 0
        for idx, (who, text, tags) in enumerate(lines):
            out.append(f"**{who}**: {text}")
            if tags: out.append("   `" + " | ".join(tags) + "`")
            if idx <= 8:
                for t in tags:
                    if t.startswith("log:") and "@L" in t:
                        warm_n += 1; warm_hits += 1 if "=hit" in t else 0
        note = await tutor.debrief(track, d, conv.messages)
        if note: db.add_note(track, d, note)
        if not any(t.startswith("day_done:OK") for _, _, tg in lines for t in tg): db.mark_day(track, d, "done")  # student presses Mark complete
        mm = db.mastery_map(track, 500)
        bl = progress.backlog(track, d + 1)
        lv = {i: sum(1 for m in mm if m["level"] == i) for i in range(5)}
        summary.append(f"day {d}: skills tracked {len(mm)}, level histogram {lv}, backlog sections {bl[1]}, uncovered today {len(progress.remaining(track, d))}, early-turn graded {warm_n} (hits {warm_hits}), todos open {len(db.open_todos(track))}")
        out.append(f"\nDEBRIEF: {note}\n"); out.append("DEBUG: " + json.dumps(conv.dbg[:30]))
        with db.conn() as c:   # advance the clock one day for spaced review
            c.execute("UPDATE mastery SET next_due = next_due - 86400, updated = updated - 86400")
    out.insert(1, "## METRICS\n" + "\n".join(summary) + "\n")
    p = ROOT / "evals" / "out" / f"{label}-chain-{track}{day}-{persona}.md"; p.write_text("\n".join(out)); print(p); print("\n".join(summary))
asyncio.run(main())
