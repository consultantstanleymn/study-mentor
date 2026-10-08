"""Validate stored L3 items: a thinking-enabled DeepSeek solves each blind (MCQ) or audits the stated answer (open); mismatches go to data/items_bad.json."""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import tutor  # noqa: E402
ITEMS, BAD = ROOT / "data" / "items.json", ROOT / "data" / "items_bad.json"

async def check(key, it, sem, bad):
    async with sem:
        opts = it.get("options") or {}
        if opts:
            q = it["stem"] + "\n" + "\n".join(f"{k}. {v}" for k, v in opts.items())
            txt = await tutor._quick("Solve this multiple choice item carefully. If it has an impossible or contradictory requirement, say so. Reply ONLY JSON: {\"answer\": \"letter\", \"flawed\": true|false}", q, 120, think=True)
            j = tutor._json(txt)
            if not j or j.get("flawed") or str(j.get("answer", "")).strip().upper()[:1] != str(it["answer"]).strip().upper()[:1]:
                bad.append(key)
        else:
            q = f"{it['stem']}\nSTATED ANSWER: {it['answer']}\nWHY: {it.get('why','')}"
            txt = await tutor._quick("Verify this problem and stated answer by careful arithmetic. Reply ONLY JSON: {\"correct\": true|false, \"solvable\": true|false}", q, 120, think=True)
            j = tutor._json(txt)
            if not j or not j.get("correct") or not j.get("solvable"):
                bad.append(key)

async def main():
    items = json.loads(ITEMS.read_text()); bad = []; sem = asyncio.Semaphore(12)
    jobs = [check(k, v, sem, bad) for k, v in items.items()]
    done = 0
    for f in asyncio.as_completed(jobs):
        await f; done += 1
        if done % 50 == 0: BAD.write_text(json.dumps(bad)); print(done, len(bad), flush=True)
    BAD.write_text(json.dumps(bad)); print("final", len(items), "bad", len(bad))
asyncio.run(main())
