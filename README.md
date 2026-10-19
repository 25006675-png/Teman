# Teman

**An AI that pays Grandma's bills in her own dialect, and can't be talked into paying a scammer.**

*One AI acts, a second AI checks, fixed rules decide, and family approves.*

Teman is an elder-first personal finance app for older Cantonese speakers in Malaysia, with a companion app for their family. It was built for the 2026 Shenzhen International FinTech Competition (Topic A: Personal Finance Assistant, theme "AI as a Financial Participant").

- **For the elderly user (Ah Por):** big text and buttons, today's spending amount, bills waiting, snap-a-bill, a weekly report, Cantonese read-aloud, voice confirm, and an assistant she can talk to.
- **For her son (Ah Ming):** approve or reject risky payments within a 2-minute window, get scam alerts, report scam accounts, set signed auto-pay limits, pause the assistant.
- **The engine:** fixed rules and a risk score decide every payment. No AI can authorise one. Every payment needs a digitally signed approval.

The bank is a **simulated sandbox**. Teman never holds money; it only sends payment requests.

Repository: <https://github.com/25006675-png/Teman>

---

## Quick start

Requires **Python 3.12** (tested on 3.12.10).

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The page shows three panels: Ah Por's phone, the Teman engine with the partner-bank sandbox, and Ah Ming's phone, plus an audit-log strip at the bottom.

The database is created on first start (`instance/dialectguard.sqlite3`), with synthetic bill history, an opening balance of RM 850 and two signed family limits. **↺ Reset demo** (or `POST /reset`) restores this state.

The core demo works with only the steps above. Three optional parts add the real photo, voice and AI features.

### Optional: bill photos (Tesseract OCR)

Install Tesseract OCR. On Windows the app looks in `C:\Program Files\Tesseract-OCR\tesseract.exe`; elsewhere it uses `tesseract` on the PATH. Set `TESSERACT_CMD` to point somewhere else. Without Tesseract, a photo returns "please retake or type it in", and the sample bills still work.

### Optional: voice confirm (Whisper)

Download the Whisper model once, before the demo (about 3 GB). The app never downloads it during a request:

```bash
python -c "from faster_whisper.utils import download_model; download_model('large-v3')"
```

Without the model, the voice button answers "Voice isn't ready on this computer yet. Please tap a button." The **simulate voice** link sends the keyword directly and works without a microphone or model. The microphone works on `localhost`; browsers block it on plain-HTTP network addresses.

### Optional: the assistant and the AI scam analyst (Google Gemini)

Create a `.env` file in the project root (it is git-ignored):

```
GEMINI_API_KEY=your-key
```

Without a key, or with `DIALECTGUARD_AI=off`, the rules decide exactly as before. The morning check runs a fixed rule-based plan, and chat replies fall back to fixed text. **Data note:** with a key, bill/message text, payee name and amount are sent to Google's Gemini API at every analysis. Use synthetic data only.

### Read-aloud

Ah Por's answers are read aloud in a natural Cantonese voice (`zh-HK-HiuGaaiNeural`) through `POST /speak`, which uses edge-tts ([tts.py](tts.py)) and works in any browser. It needs internet; the first time a sentence is spoken takes a few seconds, and repeats come from the cache in `tts_output/`. If edge-tts fails, the page falls back to the browser's own Cantonese voice, then to text only. Amounts are read as Cantonese (`RM 42.50` → 「42蚊50仙」).

**Data note:** the spoken text (including her balance and payee names) is sent to Microsoft's online voice service. Use synthetic data only.

---

## Running the tests

```bash
python -m pytest
```

**241 tests pass** (plus 23 sub-tests) in about 10 seconds. Tests use temporary databases and keys, never call the real Gemini API, and skip the real-OCR tests if Tesseract isn't installed.

| File | What it proves |
|---|---|
| `tests/test_proposal_acceptance.py` | The 8 required tests: charged once on a double confirm, stored amount can't be overridden, prompt-injected scam blocked, `ask_child` refused for blocked payments, son approves an unknown payee, timeout → `EXPIRED` with balance unchanged, budget gap shown, unrecognised voice changes nothing |
| `tests/test_mandates.py` | Stage 2 tests 9–10: tampered amount or signature rejected, replayed approval rejected |
| `tests/test_api.py` | All 5 demo scenarios end to end over HTTP, reset, basic-mode backup |
| `tests/test_transactions.py` | State machine, two confirms at the same instant, expiry boundaries, fails closed without a verifier |
| `tests/test_rules.py` | Blocking order, score bands, median rules, scam-phrase variants, decision records |
| `tests/test_agent.py` | Assistant guardrails: no approve tool, family limits, fact-check, step limit, AI down, pause switch |
| `tests/test_ai_and_community.py` | AI can only raise risk; AI outage gives the same decisions; community reports; rapid transfers |
| `tests/test_guardian_safeguards.py` | Son approves only with the right PIN; 3 wrong PINs stop the payment; he can never approve money to his own account |
| `tests/test_guardian_timer.py` | 2-minute window, one 5-minute "Call Mum" extension, silence stops the payment |
| `tests/test_photo_voice.py` | Real photo → payable card; scam text in a photo still blocked; audio kept in memory |
| `tests/test_risk_model.py` | Shadow model never changes a decision, even when it crashes or flags everything |
| `tests/test_ocr*.py`, `test_bills.py`, `test_speech*.py`, `test_tts.py`, `test_metrics.py`, `test_check_references.py` | OCR extraction, speech keyword matching, text-to-speech fallback, evaluation tools |

---

## Demo walkthrough

Press **↺** (reset, top right) first. Photos are in [demo_photos/](demo_photos/) (fake samples, made by `python make_demo_photos.py`); 📷 **影張單** opens the file picker, and each photo goes through the real OCR. Then:

1. **Balance:** hold the mic and say 「我仲有幾多錢？」. She hears her balance and how much a day lasts to month end.
2. **Pay a bill:** 📷 → `1-tnb-electricity-bill.png` (or say 「幫我交電費」). Hold **按住交錢** for 1 second; a 5-second undo runs, then it pays and the bank balance drops.
3. **Morning check:** after a reset, click **☀ Morning check**. The assistant pays TNB and Air Selangor by itself, inside the family-signed limits (TNB ≤ RM 80, Air Selangor ≤ RM 40, once a month each).
4. **Family co-sign:** 📷 → `2-klinik-baru-bill.png`. It goes to Ah Ming automatically; his screen shows the request. Tap **Approve** and enter his PIN **2580** (demo value; keyboard digits work) within the 2-minute countdown (📞 *Call Mum* adds 5 minutes once). Three wrong PINs stop the payment.
5. **Scam with a hidden instruction:** 📷 → `3-prize-scam-message.png`. Her screen turns red with no pay button; Ah Ming's banner has no approve button.
6. **Scam with no keywords:** 📷 → `4-inspector-lim-message.png`. The rules send it to Ah Ming; with a Gemini key, his screen lists the scam tactics the AI found. Tap **It's a scam: report & reject**.
7. **Proof:** open the audit log (bottom strip → *Expand*) and press **🧪 Double-tap test** with a payment waiting: two confirms at the same instant, one charge. **⇩ JSON export** downloads `/logs`.

Family can be the problem too: 📷 → `7-ah-ming-own-account-message.png` (Ah Ming asks Mum to pay his own account). His screen has no Approve button; he can't approve money going to himself.

Extra photos: `5-air-selangor-bill.png`, `6-tnb-high-bill.png` (higher than usual → a "be careful" warning). Without a microphone or photos, **打字** on her phone offers typed questions and a backup list of sample bills.

Each step leaves state behind (paid bills, used monthly limits, scam reports, the transfer count), so reset between takes.

---

## How it decides

1. **Blocking rules**, in order, stopping at the first match: missing payee or amount → scam phrase (English and Chinese, robust to spacing and hidden characters) → blacklisted payee → reported by 2+ families → over the RM 500 hard cap. Blocked payments can't be approved by anyone.
2. **Risk score** = sum of fired weights: unknown payee 40 · over the payee's auto-pay limit 40 · more than 1.2× her median bill 20 · AI scam analyst found a tactic 40 · reported by 1 family 40 · 2+ transfers in 24 h 40.
   Bands: **< 20 approve** (she confirms) · **20–40 warn** ("pay anyway") · **≥ 40 review** (her son decides).
3. **Auto-pay limit** per payee = 1.5 × her median past bill, or RM 50 with fewer than 3 past bills.
4. **Every decision is recorded** with every rule checked, the score, the band and the config version, and is exported by `GET /logs`.

All thresholds live in `data/*.json`. Full details: [PROPOSAL.md](PROPOSAL.md) §5–§6.

## Security in brief

- **The frontend only sends `tx_id`.** Amount and payee are read from the database, so they can't be changed after the fact.
- **Signed approvals (ECDSA P-256):** every payment needs a signed record that matches the stored transaction, hasn't expired (60 s) and hasn't been used. Family auto-pay limits are signed too; an edited limit stops working.
- **No AI on the decision path:** the assistant has no approve tool, picks which stored bill to pay (never the amount or payee), can pay alone only inside a signed family limit, is limited to 6 steps, logs every tool call, and can be paused by the son. Its replies are fact-checked against what its tools actually did. The AI scam analyst can only raise risk.
- **The son's approval:** a PIN checked by the server against a salted hash in `data/guardians.json` (demo PIN 2580); 3 wrong PINs stop the payment. He can never approve a payment to his own accounts (also in `data/guardians.json`).
- **Fail-safe timers:** 10 minutes for Ah Por to confirm; 2 minutes for her son (+5 once). No answer means no payment.
- **Append-only audit:** SQLite triggers refuse updates and deletes of logs and payments, and enforce allowed state changes.
- **Voice privacy:** recordings stay in memory for transcription and are never saved; logs hold no audio.

Aligned in principle with OWASP Top 10 for LLM Applications (LLM01, LLM06) and the mandate idea in Google's Agent Payments Protocol (AP2). We do not claim AP2 compliance.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | none (also read from `.env`) | Turns on the assistant and the AI scam analyst |
| `GEMINI_MODELS` | `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash` | Models tried in order when one is busy or out of quota |
| `DIALECTGUARD_AI` | `on` | `off` disables every Gemini call |
| `DIALECTGUARD_APPROVAL_MODE` | `signed` | `basic` = backup without signatures (all other checks stay on) |
| `WHISPER_MODEL` | `large-v3` | Whisper model for voice confirm (must already be downloaded) |
| `TESSERACT_CMD` | Windows default path | Path to `tesseract` |
| `DIALECTGUARD_DB_PATH` | `instance/dialectguard.sqlite3` | Database file |
| `DIALECTGUARD_KEY_DIR` | `instance/keys` | Demo signing keys (created on first use) |

## API

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/analyze` | Ah Por | Preset bill id, or typed fields → decision and `tx_id` |
| POST | `/analyze/photo` | Ah Por | Bill photo → OCR → the same checks |
| POST | `/tx/{id}/confirm` | Ah Por | `{method: tap\|voice, acknowledge_warning}` |
| POST | `/tx/{id}/cancel` | Ah Por | Cancel a pending request |
| POST | `/tx/{id}/ask_child` | Ah Por | Send a Review request to her son |
| POST | `/voice` · `/voice/audio` | Ah Por | Keyword, or a recording → confirm / cancel / "please tap a button" |
| POST | `/tx/{id}/approve` · `/reject` | Son | Decide a Review request |
| POST | `/tx/{id}/call_extension` | Son | "Call Mum": +5 minutes, once |
| POST | `/tx/{id}/report_scam` | Son | Reject and report the account to other families |
| POST | `/agent/chat` · `/agent/morning_check` | Ah Por | Talk to the assistant · daily bill check |
| POST | `/agent/pause` | Son | Pause or resume the assistant |
| GET | `/state` · `/logs` · `/budget` · `/report` · `/presets` · `/officials` | Dashboard | State, audit export, budget, weekly report, sample bills, official contacts |
| POST | `/reset` | Demo | Rebuild the demo state |

Interactive API docs: <http://127.0.0.1:8000/docs>.

## Project layout

```
app.py            API endpoints
services.py       the single analysis path (reports, AI check, rules, shadow score)
rules.py          blocking rules, risk score, decision record, message templates
transactions.py   state machine, timers, double-charge-safe execution
mandates.py       signed approvals and family limits (ECDSA P-256)
agent.py          assistant: tools, guardrails, fact-check, morning check
ai_analyst.py     AI scam analyst (escalate-only)
community.py      cross-family scam reports, rapid-transfer count
budget.py         daily limit, budget gap, weekly report
bank.py           sandbox balance
audit.py          /logs export
db.py             SQLite schema, triggers, reset
ocr.py            bill reading (Tesseract)
speech.py         Whisper transcription + keyword matching
tts.py            edge-tts Cantonese audio (tested, not yet used by the page)
risk_model.py     shadow fraud model; ml/ has the training script and model card
metrics.py, check_references.py   dialect evaluation tools
data/             all rules, limits, templates and synthetic demo data (JSON)
static/index.html the demo page (plain JavaScript, no build step)
tests/            241 tests
```

## Real vs simulated

| Part | Status |
|---|---|
| Rules, risk score, decision records, budget, state machine, timers, audit log | Real code |
| Signed approvals and family limits | Real cryptography; demo keys are held by the server |
| Assistant and AI scam analyst | Real Gemini calls on synthetic data |
| Shadow fraud model | Trained on public PaySim data; logs a score only ([model card](ml/MODEL_CARD.md)) |
| OCR and voice | Real (Tesseract, Whisper large-v3) |
| Dialect accuracy | **Not yet measured**: the evaluation tools are built, the recordings are pending |
| Bank | Simulated sandbox shaped like an Open Banking API |
| Push to the son's phone | Simulated: both phones share one backend on one screen |

## Known limitations

- Signing keys are held by the server for every role; a real launch keeps each key on that person's device.
- Cantonese templates and speech phrase lists are drafts pending native-speaker review.
- Only Cantonese is supported; Hokkien and Manglish are untested.
- The whitelist knows two billers (TNB, Air Selangor); OCR is tested on clean synthetic bill images, not real paper bills.
- Weekly per-category spending caps are not configured, so the report shows no overspending warnings.
- The synthetic history is dated before mid-October 2026, so the 7-day report only shows payments made during the demo.
- Once a request is with her son, Ah Por can't cancel it herself; it ends when he decides or the timer runs out.

## Data and credits

All user data in `data/` is synthetic. The shadow model is trained on **PaySim**, *Synthetic Financial Datasets For Fraud Detection* (E. A. Lopez-Rojas, A. Elmir, S. Axelsson, 2016), licensed CC BY-SA 4.0; the dataset is downloaded by `ml/train_risk_model.py` and is not stored in this repository. The National Scam Response Centre number (997) comes from its official page, cited in `data/officials.json`.
