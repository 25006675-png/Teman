# Teman: Security Self-Assessment Report

**2026 Shenzhen International FinTech Competition · Topic A · Deliverable 05**
Repository: <https://github.com/25006675-png/Teman> · Configuration `2026-10-17.1` · Assessed 20 Oct 2026

Related: [Technical Documentation](TECHNICAL_DOCUMENTATION.md) · [Execution Evidence](execution_evidence/README.md) · [Evaluation results](EVIDENCE.md)

---

## 1. Purpose and scope

This report describes the security controls in Teman, how payment authority is divided between people, AI components and fixed rules, what evidence supports each control, and the known risks of the current prototype. It is a **team self-assessment**, not an independent audit or a compliance certification.

Teman helps an older user review bills, manage a budget and request payments, with her son approving risky ones. The prototype uses a **simulated bank sandbox**: it does not hold or move real money. All data is synthetic.

**In scope:** input handling (photo, voice, chat, typed fields), the payment decision flow, the transaction state machine, signed approvals and family limits, guardian approval, the AI components, the audit log and data handling.
**Out of scope:** real bank integration, user authentication and device security, hosting and network security, since the prototype runs on `127.0.0.1`.

---

## 2. Security design summary

**Core principle: AI may help, but it can never authorise a payment.** Every payment is decided by fixed rules and executed only against a digitally signed approval from a person, or from a limit a person signed in advance.

Every request passes three checkpoints, and no AI controls any of them:

| Checkpoint | Control |
|---|---|
| **Input** | OCR, speech and forwarded text are **data, never instructions**. They are only searched for fields and scam phrases. Fields are validated: integer sen amounts, ISO dates, required fields, and a photo amount must match the biller's issued bill |
| **Execution** | Blocking rules and a risk score decide the outcome. A payment executes only in an allowed state, for an allowed role, with a valid signed approval, in one atomic conditional update |
| **Output** | Risk messages and buttons come from a fixed template dictionary. The assistant's own replies are fact-checked against what its tools actually did; an unsupported claim replaces the whole reply with template text |

```mermaid
flowchart TB
  U["Untrusted input<br/>bill photo · voice · forwarded message · chat"]
  subgraph C1["1 · Input checkpoint"]
    V["Text is data, never instructions<br/>field validation · photo cross-check"]
  end
  subgraph C2["2 · Execution checkpoint"]
    direction LR
    R["Fixed rules decide<br/>blocking rules + score"] --> A["Signed approval<br/>hold · PIN · family limit"] --> X["Execute once<br/>append-only log"]
  end
  subgraph C3["3 · Output checkpoint"]
    O["Fixed templates · fact-checked assistant replies"]
  end
  AI["AI scam analyst<br/>can only add risk"]
  U --> V --> R
  X --> O
  V -.-> AI
  AI -. "+40 only" .-> R
```

The frontend sends only a `tx_id`. Amount and payee are frozen in the database when the request is created, so they can't be changed after the fact.

---

## 3. Permission tiers

### 3.1 Tiers by risk

```mermaid
flowchart LR
  T1["Tier 1 · Read only<br/>budget · bills · report"] --> W1["No money moves"]
  T2["Tier 2 · Safe payment<br/>APPROVE or WARN"] --> W2["Ah Por: hold or voice<br/>Assistant or pay later: only inside<br/>a signed family limit"]
  T3["Tier 3a · Review<br/>score 40 or more"] --> W3["Only Ah Ming<br/>right PIN · within 2 min<br/>never to his own account"]
  T4["Tier 3b · Block<br/>scam phrase · blacklist<br/>2+ family reports · over RM 500"] --> W4["Nobody"]
  classDef t1 fill:#eef3fb,stroke:#2a78d6,color:#0b0b0b
  classDef t2 fill:#e3f5e3,stroke:#0ca30c,color:#0b0b0b
  classDef t3 fill:#fde9e0,stroke:#ec835a,color:#0b0b0b
  classDef t4 fill:#fbe3e3,stroke:#d03b3b,color:#0b0b0b
  class T1 t1
  class T2 t2
  class T3 t3
  class T4 t4
```

| Tier | What it covers | Who can complete it | Authorisation required |
|---|---|---|---|
| 🟢 **Tier 1: Read only** | Today's budget, bills waiting, weekly report, audit log | Ah Por, Ah Ming, the assistant | None; no money moves |
| 🔵 **Tier 2: Safe payment** (APPROVE, score < 20; WARN, 20–39) | Recognised biller, normal amount | Ah Por (1-second hold or voice 「確認」; WARN needs an explicit "Pay anyway"). The assistant or pay-later scheduler, **only** for APPROVE inside a family-signed limit | Signed present approval (60 s, single use), or a valid signed family limit |
| 🟠 **Tier 3a: Review** (score ≥ 40) | Unknown payee, over the auto-pay limit, AI-flagged tactic, reported by one family, rapid transfers | **Only** Ah Ming, with his PIN, within 2 minutes (+5 once) | PIN + signed guardian approval. Never to his own account |
| 🔴 **Tier 3b: Block** | Scam phrase, blacklisted payee, reported by 2+ families, over RM 500 | **Nobody**. Not Ah Por, not Ah Ming, not the assistant | Not possible: refused at every entry point |

### 3.2 Permission matrix by actor

This is how the tiers are enforced in code. "Role" is fixed per endpoint in the demo (see §9, R1).

| Capability | Ah Por | Ah Ming | Assistant | AI scam analyst | Pay-later scheduler | Shadow model |
|---|---|---|---|---|---|---|
| Read budget, bills, report | ✅ | ✅ | ✅ | — | — | — |
| Create a payment request | ✅ | — | ✅ via `services.analyze()` only | — | ✅ re-runs `analyze()` | — |
| Choose amount or payee | ✅ (from bill / typed) | — | ❌ picks a stored bill only | — | ❌ saved bill only | — |
| Execute APPROVE / WARN | ✅ signed | ❌ | APPROVE only, inside a signed family limit | ❌ | APPROVE only, inside a signed family limit | ❌ |
| Execute REVIEW | ❌ (can only "Ask my child") | ✅ PIN + signed | ❌ (can only `ask_family`) | ❌ | ❌ | ❌ |
| Execute BLOCK | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Raise risk | — | ✅ report scam | — | ✅ +40 only | — | ❌ |
| Lower risk / unblock | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Change limits, whitelist, rules | ❌ | Signs limits at setup | ❌ | ❌ | ❌ | ❌ |
| Pause the assistant | — | ✅ | — | — | — | — |

**Where it's enforced:**

| Rule | Code |
|---|---|
| Ah Por can execute only APPROVE/WARN; the guardian only REVIEW | `transactions._execute` (`allowed_bands`) |
| Blocked requests refused (status, outcome, risk code or fired rule) | `transactions._is_blocked`, plus `risk_code NOT IN ('SCAM_SIGNAL','OVER_HARD_CAP')` in the execution SQL |
| Guardian can't approve money to his own accounts | `transactions.pays_guardian` → `SELF_APPROVAL` |
| Assistant has no approve, add-payee or change-limit tool | `agent.TOOLS` (7 tools) |
| Automatic payment only with a valid family limit | `mandates.delegated_validator` |

### 3.3 Family limits (delegated mandates)

Demo values, signed at setup and valid 365 days: **TNB up to RM 80 per bill, once a month; Air Selangor up to RM 40, once a month.** These are synthetic setup values. An edited limit fails its signature and automatic payment stops.

---

## 4. Risk rules and decision thresholds

**Blocking rules** run first; the first match stops the decision:

| Order | Condition | Code | Outcome |
|---|---|---|---|
| 1 | Missing payee, or no positive amount | `DATA_INSUFFICIENT` | Ask again |
| 2 | Configured scam phrase (English and Chinese) | `SCAM_SIGNAL` | Block; son alerted |
| 3 | Blacklisted payee | `SCAM_SIGNAL` | Block |
| 4 | Reported by ≥ 2 distinct families | `SCAM_SIGNAL` | Block |
| 5 | Amount above RM 500 | `OVER_HARD_CAP` | Block |

**Risk score**: if no blocking rule matches, the weights of every signal that fires are added:

| Signal | Points |
|---|---|
| Payee is not an exact, normalised whitelist match | 40 |
| Amount above the payee's auto-pay limit | 40 |
| Amount above 1.2 × the median past bill | 20 |
| One family has reported the payee | 40 |
| ≥ 2 transfers in the previous 24 hours | 40 |
| AI scam analyst identifies at least one tactic | 40 |

**Bands:** below 20 → APPROVE · 20 to below 40 → WARN (explicit acknowledgement required, enforced server-side) · 40 or above → REVIEW.

- The auto-pay limit is 1.5 × the payee's median bill when at least 3 past bills exist. With fewer, the limit is RM 50 and the high-amount rule is skipped.
- Whitelist matching is exact after normalisation: a typo payee such as "Tenaga Nasinal Berhad" is **not** treated as TNB.
- Scam phrase matching normalises Unicode (NFKC), case and spacing, and also checks a letters-and-digits-only copy, catching spacing, punctuation and zero-width-character variants.
- Each decision record stores every rule checked, fired signals, weights, score, outcome, rule that stopped the check, and `config_version`.
- All thresholds live in `data/*.json`. The files must share one `config_version`, or the server refuses to start. No AI component can write to them.

---

## 5. Signed approvals, guardian approval and family limits

### 5.1 Present approvals

When Ah Por confirms or Ah Ming approves, the server issues a record signed with **ECDSA P-256 / SHA-256** over canonical JSON. The record holds: transaction ID, payee, stored amount, decision, role, method, issue and expiry time, and a 128-bit random nonce.

Before payment the server verifies, in order:

1. The signature is valid.
2. Every signed field matches the stored transaction.
3. The role is eligible for the outcome band.
4. The record is within its **60-second** validity.
5. The nonce is unused. It is consumed with a conditional update in the **same database transaction** as the payment, so a failed payment rolls it back and a replay is rejected.

```mermaid
sequenceDiagram
  participant P as Ah Por's app
  participant S as Server
  participant D as Database
  P->>S: confirm (tx_id only)
  S->>D: read stored transaction
  S->>S: sign approval (60 s, nonce)
  S->>S: verify signature and fields
  S->>D: mark nonce used, set DONE if PENDING
  alt 1 row changed
    S->>D: insert payment, log, commit
    S-->>P: paid
  else 0 rows
    S->>D: roll back
    S-->>P: refused, nothing paid
  end
```

### 5.2 Fail-safe timers

| Timer | Length | If it runs out |
|---|---|---|
| Ah Por's confirmation | 10 minutes | `EXPIRED`, nothing paid |
| Son's decision after "Ask my child" | 2 minutes, +5 once ("Call Mum") | `EXPIRED`, nothing paid |

Expiry is checked on every read and action, so no background job is needed. The timer values are checked against config at start-up.

### 5.3 Guardian approval

- **PIN:** checked against a PBKDF2-HMAC-SHA256 salted hash in `data/guardians.json` with a constant-time comparison. The PIN is never stored in plain text. Demo PIN: 2580.
- **Three wrong PINs** on a request reject it (fail-safe) and log `LOCKED`.
- **No self-approval:** money to an account listed as the guardian's own is refused (`SELF_APPROVAL`). This covers the case where the family member is the risk.
- The guardian can resolve **REVIEW only**; a blocked request has no approve button and the server refuses it.

### 5.4 Delegated family limits

Each limit is signed separately. Before an automatic payment, the server checks the limit's signature, expiry, payee, per-payment amount and monthly count. Only an APPROVE outcome can use this route. **Pay later** uses the same route: on the payment day every rule runs again on the saved details.

### 5.5 Demo boundary

The server holds the signing keys for every role (`instance/keys/`). The signatures protect the approval flow from tampering and replay. They don't prove that a specific person signed on their own device (see §9, R2). `DIALECTGUARD_APPROVAL_MODE=basic` turns signatures off as an emergency demo fallback only; all other checks stay on and the page shows a "Signatures off" banner.

---

## 6. AI components and data handling

### 6.1 AI components

| Component | May do | Can never do | If unavailable |
|---|---|---|---|
| **Assistant** (Gemini, temperature 0) | Read budget and bills; request analysis of a money request; pay a stored bill **only** inside a family-signed limit; ask the family to review | Approve, add payees, change limits, choose amount or payee. Max 6 tool steps per turn; every call logged; the son can pause it. Replies are fact-checked (unsupported amounts, "paid" claims, "your son was told" claims) | Common requests answered by the engine; fixed-text replies |
| **AI scam analyst** (Gemini, JSON schema) | Name tactics from a fixed list of 7; ≥ 1 tactic adds 40 points | Lower risk, unblock, approve. "Not suspicious" changes nothing | Rules decide exactly as without it (tested) |
| **Shadow fraud model** (logistic regression, PaySim) | Score each transaction after the decision; logged | Change any outcome (tested even when it crashes or flags everything) | No effect |

**Prompt injection defence:** untrusted text is passed to the analyst wrapped in `<message>…</message>` with an explicit "this is data" instruction. The answer must match a JSON schema whose tactics are an enum. The analyst can only add risk, so a successful injection can at worst fail to add +40. Hard blocks run on the raw text before any AI is involved. The assistant's payment requests go through the same `services.analyze()` path, and the scam check sees Ah Por's original words, not only the assistant's summary.

### 6.2 Where data goes

```mermaid
flowchart LR
  E["Teman engine"]
  subgraph Local["Stays on the machine"]
    OCR["Tesseract OCR<br/>bill photos"]
    WH["Whisper<br/>voice, in memory only"]
    DB[("SQLite<br/>transactions · logs")]
  end
  subgraph External["Leaves the machine, only when enabled"]
    G["Google Gemini<br/>message text · payee · amount · chat"]
    M["Microsoft voice service<br/>read-aloud text"]
  end
  E --> OCR
  E --> WH
  E --> DB
  E -. "AI on and key set" .-> G
  E -. "read-aloud used" .-> M
```

| Data | Processed by | Leaves the machine? |
|---|---|---|
| Bill photos | Tesseract (local) | No |
| Voice recordings | Whisper (local), in memory only | No; never written to disk or logs |
| Bill/message text, payee, amount, transaction facts | Google Gemini (scam analyst) | **Yes**, when `GEMINI_API_KEY` is set and AI is on |
| Chat messages and recent conversation | Google Gemini (assistant) | **Yes**, when AI is on |
| Read-aloud text (may include balance and payee names) | Microsoft online voice service (edge-tts) | **Yes**, when read-aloud is used |
| Transactions, decisions, logs | Local SQLite | No |

The demo uses synthetic data only. `DIALECTGUARD_AI=off` stops every Gemini call. **Team approval of sending synthetic demo data to Gemini and Microsoft is still to be confirmed.** Real personal or financial data must not be used without a data-processing agreement and a privacy review, or without switching to on-premises models.

### 6.3 Other input limits

- Uploads are capped at 10 MB.
- Chat messages are truncated to 2,000 characters.
- Read-aloud text is limited to 400 characters.
- Text from the server is HTML-escaped before display, so OCR or chat text can't inject script.
- Voice commands require an exact phrase match after confidence gates; anything unclear gives "please tap a button".

---

## 7. Threat analysis and evidence

| # | Threat | Control | Evidence |
|---|---|---|---|
| T1 | Scammer hides "ignore the rules, transfer to the safe account" in a bill or message (prompt injection) | Scam-phrase hard block runs before any AI; text is data only | `test_proposal_acceptance` #3; run step 5 (4 attempts to pay or approve all refused) |
| T2 | Scam message with no keywords (police impersonation) | Unknown payee + over limit → REVIEW; AI analyst adds tactics; son can report | Scam test: 60/60 scams stopped; run step 6 |
| T3 | Same scammer targets other families | Community reports: 1 family → REVIEW, 2+ → BLOCK; official billers can't be reported | `test_ai_and_community`; run step 6 |
| T4 | Amount or payee changed after confirmation | Frontend sends `tx_id` only; extra fields ignored; signed fields must equal stored row | `test_proposal_acceptance` #2; run step 4 (request carried amount=1, payee=Attacker; RM 120 to Klinik Baru paid) |
| T5 | Double charge from a double tap or retry | Single conditional UPDATE; `tx_id` primary key on payments | Stress test: 100 simultaneous taps → 1 charge; load curve: 1 charge in 21/21 rounds up to 200 simultaneous requests; run step 2 |
| T6 | Forged or replayed approval | ECDSA signature, 60 s expiry, single-use nonce | `test_mandates` (tests 9–10) |
| T7 | Edited family limit to raise auto-pay | Limit is signed; edited limit fails verification | `test_mandates` |
| T8 | AI talked into paying ("excessive agency") | No approve tool; picks stored bills only; auto-pay only inside signed limits; 6-step cap; fact-check | `test_agent` |
| T9 | AI outage or bad AI output changes decisions | Analyst escalate-only; outage gives identical decisions | `test_ai_and_community`, `test_risk_model` |
| T10 | Son pressured to approve a scam | Blocked requests can't be approved by anyone | `test_proposal_acceptance` #4; run step 5 |
| T11 | Son approves money to himself | `SELF_APPROVAL` refusal | `test_guardian_safeguards`; run step 7 |
| T12 | Someone guesses the son's PIN | Salted hash; 3 wrong PINs stop the request | `test_guardian_safeguards`; stress test: 30 wrong PINs → 0 charges; run step 9 |
| T13 | Approval never answered | 2-minute fail-safe → EXPIRED, balance unchanged | `test_guardian_timer`; run step 8 (real 2-minute wait) |
| T14 | Misheard dialect triggers a payment | Exact phrase match; confidence gates; amounts never from speech | Speech test: 0 wrong actions in 108 clips; run step 10 |
| T15 | Misread bill photo pays the wrong amount | Photo amount must match the biller's issued bill | Photo test: 63/63 safe (2 unsafe before the fix) |
| T16 | Rapid draining by repeated transfers | 3rd transfer in 24 h needs the son | `test_ai_and_community` |
| T17 | Audit history rewritten | SQLite triggers refuse UPDATE/DELETE of logs and payments and illegal state changes | `test_transactions`; `db_tamper_checks.json` (6/6 edits refused) |
| T18 | Accidental tap by a shaky hand | 1-second hold, then a 5-second undo before anything is sent | Demo page; screenshots 06 |

"Run step" refers to [execution_evidence/RUN_RESULTS.md](execution_evidence/RUN_RESULTS.md), produced against the real server.

The controls for T4, T5, T10–T13 and T17 are also exercised together by the randomised attack test (§8): 10,000 random actions with 0 safety rules broken.

---

## 8. Audit and test evidence

- **Audit log.** Decisions and transaction events are written to SQLite. Triggers reject updates and deletes of `logs` and `sandbox_payments` and enforce the allowed state transitions. `GET /logs` exports events and decision records as JSON (`dialectguard.logs.v1`). Logs hold no audio.
- **Automated tests.** **248 tests pass, plus 23 sub-tests** (`python -m pytest`, about 15 s). They cover API scenarios, states and expiry, double-payment protection, signature tampering and replay, scam blocking, guardian PIN and self-approval, AI behaviour and assistant guardrails, OCR, speech and shadow-model isolation. Tests use temporary databases and keys; Gemini calls are mocked.
- **Evaluation (19 Oct).** 60/60 synthetic scams stopped; 0 wrong actions in 108 Cantonese voice clips; 63/63 degraded photos handled safely; 1 charge from 100 simultaneous taps. Method and limits: [EVIDENCE.md](EVIDENCE.md).

  ![Scam test: 60 of 60 scams stopped](figures/1-scam-outcomes.png)
- **Randomised attack test (Monte Carlo, 20 Oct).** 200 random sequences of 50 actions on the real app, with the AI off and a controllable clock. The actions are random requests (including hidden scam phrases and every threshold edge), taps, voice words, right and wrong PINs, simultaneous bursts, cancels, reports, pay later, requests carrying a different amount, and jumps in time. After every action, 11 safety rules were checked against the database: **0 broken in 10,000 actions** (110,000 checks), with 412 legitimate payments and 0 server errors. To prove the test can fail, it was re-run with the PIN check and then the own-account check switched off; both runs reported violations. Details: [Technical documentation §8.3](TECHNICAL_DOCUMENTATION.md#83-randomised-attack-test-monte-carlo).

  ![Randomised attack test](figures/9-randomised-attack-test.png)

- **Execution evidence.** A scripted run of every demo scenario against the real server, with its `/logs` export: [execution_evidence/](execution_evidence/README.md).

The audit log is evidence of demo execution. It is **not** an externally anchored, tamper-proof record: someone with file access can replace the database file, and the demo's `/reset` rebuilds it (R5).

---

## 9. Known risks and prototype boundaries

These are limits of the prototype, not gaps in the safeguards shown above. Severity is the impact if the prototype were exposed as-is.

| # | Risk | Severity | Current mitigation | Production fix |
|---|---|---|---|---|
| R1 | **No login.** Endpoints assume a role from the action called; anyone who can reach the server can call the son's endpoints (still needs his PIN to approve) | High | Run on `127.0.0.1` only; PIN on approvals | Per-role authentication; son's app on his own device |
| R2 | **Server holds all signing keys.** Signatures prove integrity, not that a person signed on their device | High | Keys stay local in `instance/keys/` | Keys in each user's device secure enclave, unlocked by biometrics |
| R3 | **No CSRF or rate limiting.** A malicious webpage open in the same browser could POST to `127.0.0.1:8000/reset`. Transaction IDs are 128-bit random, so payment endpoints can't be targeted without one | Medium | Demo machine only; synthetic data | Auth tokens, CSRF protection, rate limits, remove `/reset` |
| R4 | **Wrong-PIN count is per request and kept in memory.** It resets on restart; a 4-digit PIN allows 3 guesses per new review request | Medium | Each guess needs a new review request that Ah Por starts; all logged | Per-guardian lock-out stored in the database; device biometrics |
| R5 | **Audit log not externally anchored.** `/reset` and file access can replace it | Medium | Triggers stop in-app edits; JSON export after each run | Write-once storage or an external hash anchor |
| R6 | **External AI and voice processing.** Gemini and Microsoft receive text when enabled (§6.2); team approval pending | Medium | Synthetic data; `DIALECTGUARD_AI=off` | Data agreement or on-premises models |
| R7 | **Detection not validated on real data.** Whitelist has 2 billers; thresholds and scam phrases are demo values; scam test uses synthetic messages; AI flags 7% of genuine review cards | Medium | Unknown payees default to REVIEW (fail-safe); AI can only add risk | Real scam corpus; bank biller directory; threshold tuning |
| R8 | **Speech and OCR tested on synthetic inputs.** Computer voices and computer-made bill images | Low | Speech can't set amounts; photo cross-check; buttons always available | Consented recordings from elderly speakers; real bill photos |
| R9 | **Basic approvals mode** turns signatures off | Low | Off by default; banner shown; other checks stay on | Remove from production builds |
| R10 | **Simulated bank and push.** Payments are a sandbox ledger; both phones share one backend | — (scope) | Clearly labelled in the UI and docs | Bank partner or licensed payment provider |
| R11 | **Ah Por can't cancel once a request is with her son** | Low | It ends when he decides or after 2 minutes | Team decision pending |

---

## 10. Standards alignment

| Standard | How Teman relates | Claim |
|---|---|---|
| **OWASP Top 10 for LLM Applications** | LLM01 Prompt Injection: untrusted input treated as data, hard blocks before AI, schema-constrained analyst. LLM06 Excessive Agency: assistant has no approve tool, signed limits, step cap, pause switch, fact-check | Addressed in design; not a certification |
| **Google Agent Payments Protocol (AP2)** | Same principle as AP2 mandates: the AI pays only against a signed, limited, verifiable authorisation from a person | Same principle; **not** an AP2 implementation or compliance claim |
| **Open Banking payment initiation** | Request → challenge (signed approval) → execute pattern | Conceptual only; no real bank integration |

---

## 11. Conclusion

Teman separates AI assistance from payment authority:

- Deterministic rules make every risk decision.
- Hard blocks can't be overridden by anyone.
- Eligible payments need a signed, single-use approval or a valid family-signed limit.
- Amounts and payees are frozen at creation.
- The audit trail is enforced by the database.

These controls are shown by 248 automated tests, by a randomised attack test (10,000 random actions, 0 safety rules broken), by evaluation on synthetic scams, voices and photos, and by a scripted run against the real server.

The main gaps before any real deployment:

1. Authentication per role
2. User-held signing keys
3. CSRF protection and rate limiting
4. An externally anchored audit log
5. Validation on real scam messages, real bills and elderly speakers
