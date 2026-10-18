# Integration Required

**Project:** DialectGuard  
**Updated:** 17 Oct 2026

**Handoff order:** Technical Lead -> Tech 2 integration -> Tech 1 -> Tech 2 validation -> Tech 2 closeout.

## 1. Technical Lead -> Tech 2

### Supplied by the Technical Lead (18 Oct, branch `integrate-core`)

- **`app.py`:** every §7 endpoint wired to Tech 2's services. Roles are bound per endpoint (`AH_POR`: analyze/confirm/cancel/ask_child/voice; `GUARDIAN`: approve/reject). Errors return `{error, message}` with Tech 2's HTTP statuses. Run with `uvicorn app:app`.
- **Startup/reset:** the app initializes the DB on startup; `POST /reset` drops and rebuilds all tables (`db.reset_database()`).
- **Balance:** `bank.py` = `data/bank.json` opening balance (RM 850, synthetic) minus sandbox payments.
- **Warn:** "Pay anyway" = `POST /tx/{id}/confirm` with `{"acknowledge_warning": true}`.
- **Signed approvals:** `mandates.py` (ECDSA P-256, canonical record, field/role/expiry checks, single-use nonce). The server signs at confirm/approve time and verifies inside the payment transaction. Backup without signatures: `DIALECTGUARD_APPROVAL_MODE=basic`.
- **Tests:** `tests/test_mandates.py` (Stage 2 tests 9–10 with real signatures) and `tests/test_api.py` (all 5 demo scenarios, core tests 2/6/8 over HTTP, reset).

### Technical Lead: still to provide

1. **Frontend:** connect the mockup to `/state`, `/logs`, `/budget`, `/report` and the transaction actions. The UI must render server decisions, not recompute them.
2. **Cancel after `ask_child`:** currently not allowed (only `PENDING` can be cancelled). Confirm this is intended.
3. **Delegated (per-biller) mandates:** not implemented; only per-payment signed approvals exist.
4. **Audit anchor (optional Stage 2):** own `anchor.py` and testnet work. This must not block the core demo.

### Tech 2: next actions

1. **Review the integration:** check `app.py` and `mandates.py` against your service contract.
2. **Map responses in the frontend contract:** incomplete analysis (`tx_id: null`) goes to the retake/type-in flow.

## 2. Tech 1 -> Tech 2

### Tech 1: provide these items

1. **OCR contract:** canonical payee ID/name, integer `amount_minor`, due date, original extracted text for scam-keyword scanning, and missing/invalid-field behavior.
2. **Validated bill fixtures:** synthetic normal TNB, unusually high, typo, and scam/prompt-injection bills with expected extracted fields.
3. **Speech command contract:** exactly `confirm`, `cancel`, or no-match (`null`/"say again"). Speech never supplies or changes a payment amount/payee.
4. **Dialect evaluation:** named models, sample size, measured accuracy, and error analysis using consented recordings.
5. **Audio retention:** decide what happens to audio after transcription. Never write raw audio to audit logs.

### Tech 2: next actions

1. **Validate OCR inputs:** check the supplied fields against `transactions.analyze_transaction()` and `rules.evaluate_transaction()`; update provisional fixtures only from confirmed data.
2. **Connect speech commands:** route recognized commands through `transactions.dispatch_voice_keyword()`. Verify no-match leaves state/payment unchanged and returns the fixed "please tap a button" template. Tech 2 does not implement ASR or keyword recognition.
3. **Update report evidence:** use only measured results and the confirmed (or unresolved) audio-retention policy. Do not invent accuracy claims or privacy behavior.

## 3. Team Decisions

- **AI scam analyst (Gemini, added 18 Oct):** bill/message text, payee name and amount are sent to Google's Gemini API at every `/analyze`. Approve this for the demo (synthetic data only) and state it in the Security self-assessment. Without a key, or with `DIALECTGUARD_AI=off`, the rules run alone.
- **New thresholds (demo values in `data/limits.json`, pending team approval):** community reports: 1 family = son review, 2+ families = block. Rapid transfers: 2+ transfers in 24 hours means the next one needs the son.
- **Cantonese review:** also covers the new templates (`AI_SCAM_SUSPECTED`, `COMMUNITY_REPORTED`, `RAPID_TRANSFERS`) and `ai_tactic_labels` in `data/messages.json`.
- **Spec change to approve:** on equal weight, the new warnings (community report, AI, rapid transfers) are shown to Ah Por before `PAYEE_UNKNOWN`. Update PROPOSAL §6b if accepted.

- **Weekly category caps:** `data/budget.json` has no approved `weekly_category_budgets_minor`. Provide category limits before automatic high-spend warnings/suggestions are enabled; the proposal does not define a numerical "clearly high" threshold.
- **Category attribution:** current report mapping uses whitelist categories; unmatched bills are `uncategorized`, transfers are `transfers`. Confirm whether more categories or another trusted source are needed.
- **Budget fixtures:** income split/arrival dates, fixed costs, and medical/utilities reserves are synthetic assumptions. Approve or replace them before presenting as Ah Por's actual budget.
- **Report dates:** confirm seven-day week start and Malaysia-local versus UTC date boundaries for `/report` and `/budget`.
- **Official bank contact:** supply the bank and its published contact source before adding a hotline. Never invent one; NSRC 997 is separately sourced in `data/officials.json`.
- **Delegated biller limits:** family setup must supply signed per-biller limits before Tier 2 can be described as using delegated authorization.
- **Cantonese templates:** a native speaker must review `data/messages.json`; Slides/reviewer supplies corrections, and Tech 2 updates the templates.

## 4. Tech 2 Closeout Steps

After the Lead's API contract and Tech 1's input contracts arrive, Tech 2 should:

1. Wire the Tech 2 services to the agreed API while preserving server-owned transaction values and decisions.
2. Replace provisional OCR fixtures/config only with Tech 1's validated data; pass canonical speech commands through the dispatcher.
3. Integrate the Lead's verifier and balance source when available, then run the real Stage 2, expiry/balance, and end-to-end API tests. Until then, describe verifier-boundary tests as simulations and balance assertions as incomplete.
4. Draft/finalize the Security self-assessment with implemented controls, three checkpoints, permission tiers, risk scoring, signed-approval status, known risks, and standards alignment. It can be drafted before integration; mark Lead/Tech 1 evidence as pending until verified.
5. Run the full suite after integration and provide the ordered `/logs` JSON as execution evidence.

## Role Ownership

- **Tech 1:** OCR, speech recognition/keyword matching, TTS, preset bills, dialect evaluation.
- **Technical Lead:** API contract/routes, startup/reset, frontend, signed approval implementation, optional on-chain anchor.
- **Tech 2:** database/config, rules, budget/report logic, transaction business logic, tests, log export, integration of its logic into the agreed API, and Security self-assessment.
- **Slides:** citations/deck and native Cantonese review. The Security self-assessment is a separate deliverable; slides/video may reuse its summary but do not replace it.

## Handoff Rule

For each new DialectGuard request, check current files and update this checklist. Keep unresolved items listed; delete an item after its owner supplies the required contract, data, or decision. Never fill gaps by assumption.