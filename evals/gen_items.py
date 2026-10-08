"""Pre-generate one exam-tier (L3) item per teachable section, with named traps. Resumable; writes data/items.json. DeepSeek only."""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import content, tutor, progress  # noqa: E402
OUT = ROOT / "data" / "items.json"

STYLE = {
 "aws": ("AWS Solutions Architect Professional (SAP-C02) scenario item: a 3 to 4 sentence business scenario with stated constraints (cost, operational overhead, compliance, RTO/RPO, scale), "
         "a question stem asking for the BEST solution, and FOUR options A-D. Exactly one is best. Each wrong option must be a plausible near miss that fails one stated constraint; name the trap in 2-5 words "
         "(e.g. 'solves the wrong layer', 'too much operational overhead', 'ignores the explicit deny'). Do not rely on service limits or prices you are unsure of."),
 "lsat": ("ORIGINAL LSAT Logical Reasoning item (never reproduce real test text): a 4 to 6 sentence stimulus, one question stem fitting the skill (e.g. main conclusion, assumption, flaw, strengthen), "
          "and FIVE options A-E. Exactly one is correct. Each wrong option must be a named LSAT trap (e.g. 'opposing view', 'premise restatement', 'sub-conclusion', 'too strong', 'out of scope', 'reversal')."),
 "quant": ("Quant finance interview-style problem: a concrete puzzle or short calculation with small numbers testing the skill (include a common pitfall such as look-ahead bias, survivorship, "
           "or confusing simple and log returns). No options: give the exact answer and the most common wrong answer with why it tempts."),
}

async def one(track, day, sid, title, text, skills, sem, store):
    key = f"{track}:{day}:{sid}"
    if key in store: return
    async with sem:
        sysmsg = (f"Write ONE exam-tier practice item. {STYLE[track]} Test these skills: {', '.join(skills)}. Base it strictly on the lesson text; stay correct and unambiguous. "
                  "Reply ONLY JSON: {\"stem\": \"the full stimulus plus question\", \"options\": {\"A\": \"..\", ...} or {} for quant, \"answer\": \"letter or exact answer\", "
                  "\"why\": \"2 sentences why right\", \"traps\": {\"A\": \"trap name and why it tempts\", ...}}")
        txt = await tutor._quick(sysmsg, f"SECTION: {title}\n\n{text[:6000]}", 900)
        j = tutor._json(txt)
        if j and j.get("stem") and j.get("answer"): store[key] = j

async def main():
    store = json.loads(OUT.read_text()) if OUT.exists() else {}
    sem = asyncio.Semaphore(12); jobs = []
    for track, meta in content.TRACKS.items():
        for day in range(1, meta["days"] + 1):
            d = content.load_day(track, day)
            if not d: continue
            sm = progress.section_meta(track, day)
            for s in d["sections"]:
                m = sm.get(s["id"], {})
                if m.get("kind") == "teach" and m.get("skills") and len(s["text"]) > 400:
                    jobs.append(one(track, day, s["id"], s["title"], s["text"], m["skills"], sem, store))
    print(len(jobs), "items to (maybe) generate", flush=True)
    done = 0
    for f in asyncio.as_completed(jobs):
        await f; done += 1
        if done % 100 == 0: OUT.write_text(json.dumps(store)); print(done, len(store), flush=True)
    OUT.write_text(json.dumps(store)); print("final", len(store))
asyncio.run(main())
