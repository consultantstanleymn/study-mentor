"""One-time (re-runnable) generator: 2-4 named skills per lesson section, cached in data/skills.json. DeepSeek only."""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import content, tutor  # noqa: E402

OUT = ROOT / "data" / "skills.json"

async def one(track, day, sem, store):
    async with sem:
        d = content.load_day(track, day)
        if not d: return
        key = f"{track}:{day}"
        if key in store: return
        secs = [{"id": s["id"], "title": s["title"], "text": s["text"][:1800]} for s in d["sections"]]
        listing = "\n\n".join(f"[{s['id']}] {s['title']}\n{s['text']}" for s in secs)
        sysmsg = ("For each lesson section, name 2 to 4 testable SKILLS a learner must own (short noun phrases, 2-5 words, stable and specific, "
                  "e.g. 'SCP region restriction', 'Necessary vs sufficient', 'Log return additivity'). Skip sections like sources, preview, recap. "
                  "Also label each section kind: 'teach' (concepts), 'assignment' (setup, lab, homework, action items to do offline) or 'practice' (practice items/drills to attempt). "
                  "Reply ONLY JSON: {\"sections\": {\"<id>\": {\"kind\": \"teach|assignment|practice\", \"skills\": [..]}}}")
        txt = await tutor._quick(sysmsg, listing[:20000], 1500)
        j = tutor._json(txt)
        if j and "sections" in j: store[key] = j["sections"]

async def main():
    store = json.loads(OUT.read_text()) if OUT.exists() else {}
    sem = asyncio.Semaphore(8)
    jobs = []
    for track, meta in content.TRACKS.items():
        for day in range(1, meta["days"] + 1): jobs.append(one(track, day, sem, store))
    await asyncio.gather(*jobs)
    OUT.write_text(json.dumps(store)); print(len(store), "days")
asyncio.run(main())
