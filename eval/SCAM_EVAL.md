# Scam-detection evaluation (18 Oct 2026)

**Question:** how well does Teman stop scam payments, and how often does it get in the way of genuine ones? And what does the second AI (the Gemini scam analyst) add on top of the fixed rules?

**Run it:** `python eval/run_scam_eval.py` (rules + AI; needs `GEMINI_API_KEY`) or `python eval/run_scam_eval.py --rules-only` (no network). Per-item results: `eval/results/scam_eval_results.json`. The AI's answers are cached in `eval/results/ai_cache.json`, so a re-run reproduces these numbers without calling the API.

## Method

- **Test set:** `eval/scam_messages.json`, **120 labelled payment requests: 60 scams, 60 genuine.** They are synthetic and were written by a separate AI assistant that was told not to open the repository, so it never saw the rules, keyword lists or thresholds.
  - The scams cover 28 scam categories: Macau-style police/court/BNM/LHDN impersonation, parcel and customs fees, "account frozen" and e-wallet verification, prize and refund advance fees, investment and crypto, job/task scams, loan fees, romance, "Mum, I changed my number", fake hospital emergency, fake sellers, fake charity, fake TNB/Air Selangor notices, "safe account", and fake government aid. Three contain hidden instructions aimed at an AI assistant.
  - **38 of the 60 scams are RM 500 or less**, so the RM 500 hard cap can't do the work for them.
  - The genuine items are ordinary TNB and Air Selangor bills, other bills (Indah Water, Astro, Unifi, Maxis, condo), and payments to family, tuition, temple, church, clinic, market stall, plumber, friends, the residents' association and funeral contributions. Many use words like "urgent", "verify", "account", "refund", "prizes" or "pay today", because real messages do.
  - Languages: English 48, Malay 31, Chinese 19, Cantonese 11, mixed 11.
- **Path:** each request goes through the app's own `services.analyze()` on a fresh synthetic demo database: the same blocking rules, risk score and AI check as a real request. Known billers get their standard name, as the bill-reading step would give them.
- **Two runs:** rules only (AI switched off), and rules + AI (`gemini-3.5-flash-lite`, temperature 0; it answered all 120).
- **"Stopped"** means the payment can't go through without her son (Review) or at all (Block).

## Results

| Measure | Rules only | Rules + AI |
|---|---|---|
| Scams stopped (blocked or sent to her son) | **60/60 (100%)** | **60/60 (100%)** |
| … blocked outright | 23/60 (38%) | 23/60 (38%) |
| Scams of RM 500 or less stopped | 38/38 (100%) | 38/38 (100%) |
| Scams that would go through on her tap alone | 0/60 | 0/60 |
| Genuine TNB / Air Selangor bills payable on her tap alone | 16/18 (89%) | 16/18 (89%) |
| Genuine payments to other payees sent to her son | 41/42 (98%) | 41/42 (98%) |
| Genuine payments wrongly blocked | 1/60 (2%) | 1/60 (2%) |

**What the second AI adds.** The AI never changes a decision (by design it can only add risk, and these requests were already going to her son). What it changes is **what her son sees**:

| | Scams | Genuine |
|---|---|---|
| AI named at least one scam tactic | **58/60 (97%)** | **4/60 (7%)** |
| … on official TNB / Air Selangor bills | — | 0/18 |
| Review cards that reach the son **with** the AI's tactic warning | 35 scams | 3 genuine |
| Review cards that reach the son **without** it | 2 scams | 38 genuine |

So when a request reaches Ah Ming with the AI's warning, **35 of 38 (92%) are scams**; without the warning, **2 of 40 (5%)** are.

All three hidden "AI assistant: approve this" instructions failed: the AI flagged all three, and the rules stopped all three.

## How to read this

1. **The 100% comes from a strict allowlist, not from clever detection.** Every payment to anyone other than the two registered billers goes to her son. That stops all 60 scams, but it also sends 41 of 42 genuine payments to other payees to him. With rules alone, **37 scams and 41 genuine requests reach the son with the same message**, "We don't recognise this payee". He can't tell them apart from the card.
2. **The second AI is what separates them.** Its tactic warning appears on 35 of the 37 scam review cards and only 3 of the 41 genuine ones. This is the real case for the AI: it makes the son's decision informed, without ever being able to approve anything.
3. **Fixed rules alone catch few scams by content.** The scam-phrase rule blocked only 4 of 60 scams; the RM 500 hard cap blocked 19. The rest were stopped only because the payee was unknown.
4. **Genuine bills mostly flow.** 16 of 18 genuine TNB/Air Selangor bills can be paid on her tap (3 of them with a "more than usual" warning). The two that went to her son were unusually high TNB bills (RM 128.60 with arrears, RM 147.85 in a school-holiday month).
5. **One genuine payment was blocked outright:** a RM 1,280 annual insurance premium that her son had checked (g025), stopped by the RM 500 hard cap. Nobody can override that cap in the current design, so large genuine payments need another channel (e.g. the bank branch or the son paying).

## Errors

**Scams the AI missed** (both still went to her son as unknown payees):
- s049: a durian pre-order from a fake seller (Malay, RM 225)
- s053: a flood-relief appeal into a trading account (Malay, RM 30)

**Genuine requests the AI flagged** (none changed the outcome):
- g025: insurance premium, RM 1,280 ("urgency"); blocked by the hard cap anyway
- g028: real son asking for RM 200 to his wife's account after his ATM card was swallowed ("impersonation", "urgency", "unusual payee")
- g029: grandson's study send-off money, RM 300 ("impersonation")
- g053: neighbours pooling funeral money, RM 50 ("impersonation", "urgency")

The false alarms are family-style requests that look like the "it's me, send money" scam. That is the hard case, and exactly where a son's judgement belongs.

## Limits (say these with the numbers)

- **Synthetic and small:** 120 items, written by an AI, not collected from real victims. Real scams may be harder.
- **AI-written test, AI detector:** messages written by one AI may be easier for another AI to recognise than real ones. The rules-only numbers don't have this problem.
- **One run:** a single model (`gemini-3.5-flash-lite`) at temperature 0, on 18 Oct 2026. Results can change with model versions.
- **The test set wasn't tuned to the rules,** but the rules were written by the same team that commissioned the test set.
- **No real users:** this measures the decision engine, not whether Ah Por or her son act on the warning.

**Safe claim for the deck:** *"On 120 synthetic test requests written independently of our rules, Teman stopped all 60 scams before money moved. Its second AI flagged 58 of the 60 scams and 4 of the 60 genuine requests, so the son can tell them apart. Synthetic data; real-world performance not yet measured."*
