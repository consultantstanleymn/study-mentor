# Evaluation history

Teaching quality was scored on the ten criteria in `RUBRIC.md` by an independent reviewer reading simulated sessions (four single-day runs plus five-day chains). Transcripts are generated locally into `evals/out/` and are not committed.

| Iteration | Overall | Main change |
|---|---|---|
| 0 | 3.5 | Baseline: quizzing from the first minute, no memory across days |
| 13 | 6.5 | Grader-first scoring, server-owned practice items, coverage gating, carry-forward |
| 14 | 6.5 | Multi-day chains, review-worthy warm-ups, honest-grade cleanup |
| 15 | 6.5 | Hollow-session backlog fix, review items for due skills, quant sitting length |

Simulated students cannot measure an exam score. Reaching the top of the scale needs real use: timed mixed sets, mock exam score logging and weeks of trend data.

Iteration 16 (final, not re-scored): missing-passage guard so a student who cannot see content is never graded, consolidation sittings when the backlog exceeds two sections, open homework checked at the next opening, and a trailing-question retry. Turns without a question dropped from about 40% to under 10% in the four standard sessions, and quant graded answers rose from 5 to 17 per session.
