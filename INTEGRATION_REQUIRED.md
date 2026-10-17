# Integration Required

**Project:** DialectGuard  
**Updated:** 17 Oct 2026

**Handoff order:** Technical Lead -> Tech 2 integration -> Tech 1 -> Tech 2 validation -> Tech 2 closeout.

## 1. Technical Lead -> Tech 2

### Technical Lead: provide these items

1. **API contract and routes:** endpoint request/response fields, error mapping, role binding, and the `app.py`/FastAPI route skeleton.
2. **Startup and reset:** database connection ownership, seed data, and `/reset` behavior.
3. **Balance evidence:** source and test/reset interface for the current sandbox balance.
4. **Frontend contract:** `/state`, `/logs`, `/budget`, `/report`, and transaction actions. The UI must render server decisions, not recompute them.
5. **Warn/Review behavior:** represent Warn's explicit "Pay anyway" confirmation; clarify whether Cancel is allowed after `ask_child`.
6. **Signed approvals (Stage 2):** implement `mandates.py` using ECDSA P-256, canonical signed records, field/role/expiry checks, and one-time nonce enforcement. Payments remain fail-closed until supplied.
7. **Audit anchor (optional Stage 2):** own `anchor.py` and testnet work. This must not block the core demo.

### Tech 2: next actions

1. **Wire services:** connect Tech 2's transaction, budget, report, and audit-export services to the agreed routes; replace fake endpoint behavior without changing the Lead-owned contract.
2. **Keep values server-owned:** accept `tx_id` plus only agreed method/acknowledgement fields. Load amount, payee, decision, expiry, and status from the database; bind `AH_POR`/`GUARDIAN` roles server-side.
3. **Map responses:** use agreed HTTP statuses and error shapes; map incomplete analysis (`tx_id: null`) to the retake/type-in flow.
4. **Wire `/logs`:** use `audit.build_logs_export()` or `audit.export_logs_json()` and preserve event order and decision records.
5. **Integrate signed approvals:** once the real verifier is supplied, pass it to the service and test signature, tampering, expiry, role, and replay checks. Never use a permissive callback.
6. **Verify timeout balance:** once the balance source is supplied, assert both `EXPIRED` and unchanged account balance.
7. **Run integration tests:** test the endpoints, reset path, and full Tech 2 suite against the integrated app.

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