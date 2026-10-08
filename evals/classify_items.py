"""Attach the primary tested skill to every stored item (item['skill']), chosen from the section's skill list."""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import tutor, progress  # noqa: E402
P = ROOT / "data" / "items.json"
async def one(key, it, sem):
    track, day, sid = key.split(":", 2)
    skills = progress.section_meta(track, int(day)).get(sid, {}).get("skills", [])
    if not skills or it.get("skill"): return
    async with sem:
        t = await tutor._quick("Which ONE of these skills does the item primarily test? Reply ONLY JSON {\"skill\": \"<exact name from list>\"}", f"SKILLS: {skills}\nITEM: {it['stem'][:1200]}", 40)
        j = tutor._json(t)
        if j and j.get("skill") in skills: it["skill"] = j["skill"]
async def main():
    items = json.loads(P.read_text()); sem = asyncio.Semaphore(12)
    await asyncio.gather(*[one(k, v, sem) for k, v in items.items()])
    P.write_text(json.dumps(items)); print(sum(1 for v in items.values() if v.get("skill")), "classified of", len(items))
asyncio.run(main())
