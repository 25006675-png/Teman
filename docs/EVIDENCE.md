# Teman: Security & Quality Evidence

Results of the tests run on 19 Oct 2026 (decision map, randomised attack test and load curve: 20 Oct), with charts for the security report (`docs/figures/`).
Every number below can be reproduced with the commands at the end.

## At a glance

| What we tested | Result | Chart |
|---|---|---|
| **Scam requests** (60 scams, 60 genuine, written independently of our rules, in 4 languages) | **60 of 60 scams stopped** before money moved: 23 blocked outright, 37 sent to her son | `1-scam-outcomes.png` |
| **The second AI's warning** on the son's review cards | On **95%** of scam cards (35/37) and **7%** of genuine ones (3/43) | `2-ai-warning-on-son-cards.png` |
| **Spoken Cantonese** (108 clips: 18 sentences × 3 Hong Kong voices × quiet/noisy room) | **0 wrong actions.** Everyday requests 42/42 · voice commands 28/30 (the 2 misses were "didn't catch that") · amounts 31/36 | `3-speech-accuracy.png` |
| **Bad bill photos** (7 photos × 9 degradations: blur, tilt, dim light, low resolution, compression, noise) | **63 of 63 handled safely** after one flaw our test found was fixed (see below) | `4-photo-robustness.png` |
| **Speed** (team laptop, CPU only) | Rules decision **14 ms** · photo reading **0.2 s** · new spoken reply **1.4 s** (repeats instant) · speech recognition **5.2 s** | `5-latency.png` |
| **Simultaneous requests** (real server) | 100 taps → **1 charge** · 50 approvals → **1 charge** · 30 wrong PINs → **0 charges**, payment stopped | `6-stress-test.png` |
| **Automated tests** | **248 pass** (plus 23 sub-tests), grouped by the property they protect | `7-tests-by-property.png` |
| **Decision map** (real rules engine, every amount RM 0.01–1,000, 8 situations) | Cut-offs exact to the sen: TNB warns above RM 46.08, goes to her son above RM 57.60; every unknown payee goes to her son; scam text, 2+ family reports and anything over RM 500 are blocked | `8-decision-map.png` |
| **Randomised attack test** (Monte Carlo: 200 random sequences × 50 actions on the real app, AI off, fixed seed) | **0 of 11 safety rules broken** in 10,000 actions (110,000 checks); 412 legitimate payments; 0 server errors. Self-check: with the PIN check or the own-account check switched off, the same test reports violations | `9-randomised-attack-test.png` |
| **Load curve** (real server, 1 → 200 simultaneous requests, 3 rounds each) | **Exactly 1 charge in 21 of 21 rounds**, 0 server errors. Response time grows with load on one process and one SQLite file: new bill check median 80 ms alone, 1.0 s at 100, 5.1 s at 200 | `10-load-curve.png` |

## Found and fixed by these tests

1. **Misread amounts on tilted bill photos.** A photo of the Air Selangor bill (RM 19.20) tilted 3° was read as RM 49.20, still within that biller's limits, so it would have been offered for payment (she sees the amount before holding to pay, but might not notice). **Fix:** for a recognised biller, the amount read from a photo must match the bill that biller issued (in production, the biller's own e-bill, through the bank's bill-presentment feature gives this amount); if not, she's asked to check and the misread is never used. **Re-test:** 0 unsafe results in 63 (before: 2).
2. **A question treated as a request.** 「電費交咗未呀？」 ("has the electricity been paid?") opened the payment sheet. **Fix:** questions are no longer treated as "pay electricity". Nothing could have been paid without her hold.
3. **Two valid Cantonese spellings not recognised.** Whisper writes 「仲淨幾多錢」 for 「仲剩幾多錢」, and sometimes 「付款」 for 「俾錢」. Both are now understood. (First scoring before these were added: everyday requests 36/42, commands 27/30. Kept in `eval/results/speech_eval_results_first_scoring.json`.)

## Why a misheard amount can't cost money

Whisper sometimes formats spoken amounts wrongly: 「十九蚊二毫」 (RM 19.20) became "19.02" in 5 of 108 clips. This never reaches a payment: spoken words only choose *which* stored bill to act on, and the screen only ever sends a transaction ID. Every amount paid comes from the stored transaction, and every payment still needs her hold or her son's PIN.

## Method notes and limits (say these if asked)

- **Scam test:** synthetic requests written by a separate AI that never saw the rules; one model run of the AI check. Real scam messages are not yet tested. Details: `eval/SCAM_EVAL.md`.
- **Speech test:** Microsoft's computer voices (edge-tts), which are clearer than a 72-year-old speaker; the noise is synthetic. Real recordings from elders are the first pilot measure (`eval/references.csv`).
- **Photo test:** the demo photos are clean computer-made images, degraded in software; real phone photos of real bills are not yet tested.
- **Speed:** one laptop, no graphics card. A bank server with a graphics card would make speech recognition about a second.
- **Stress test and load curve:** one server process and a single database file; a production deployment would use the bank's database. Timings vary between runs.
- **Randomised attack test:** in-process on the real endpoints with the AI off and a controllable clock (so timers can run out without waiting). It checks the 11 rules listed under the chart; it doesn't test the AI's judgement, OCR or speech.

## Reproduce

```bash
python eval/run_scam_eval.py --rules-only   # or without the flag, with GEMINI_API_KEY set
python eval/run_speech_eval.py              # ~15 min; --rescore re-scores saved transcripts
python eval/run_photo_eval.py
python eval/run_stress_test.py
python eval/run_latency.py                  # after the three above
python eval/run_decision_map.py
python eval/run_invariant_fuzz.py           # ~5 min; add --break pin to see it catch a broken safeguard
python eval/run_load_curve.py
python eval/make_figures.py                 # charts -> docs/figures/
python -m pytest
```
