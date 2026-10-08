"""Multi-day continuity sim: run day N, debrief, then day N+1 in the same throwaway DB. Usage: sim_multi.py label track day persona [days=2]"""
import asyncio, os, random, sys
import sim
from sim import db, tutor, progress, run, ROOT

async def main():
    label, track, day, persona = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    days = int(sys.argv[5]) if len(sys.argv) > 5 else 2
    key = tutor.api_key(); turns = int(os.environ.get("SIM_TURNS", "22"))
    out = [f"# {label} multi-day {track} from day {day} persona={persona}\n"]
    for d in range(day, day + days):
        conv, lines = await run(track, d, persona, turns, "teach", random.Random(label + str(d)), key)
        out.append(f"\n## DAY {d}\n")
        for who, text, tags in lines:
            out.append(f"**{who}**: {text}")
            if tags: out.append("   `" + " | ".join(tags) + "`")
        note = await tutor.debrief(track, d, conv.messages); db.add_note(track, d, note)
        out.append(f"\nDEBRIEF: {note}\nUNCOVERED: {[x['title'] for x in progress.remaining(track, d)]}\nMASTERY: {db.mastery_map(track)}\nDUE: {db.due_topics(track)}\n")
        db.mark_day(track, d, "done")  # student presses Mark complete either way
    p = ROOT / "evals" / "out" / f"{label}-multi-{track}{day}-{persona}.md"; p.write_text("\n".join(out)); print(p)
asyncio.run(main())
