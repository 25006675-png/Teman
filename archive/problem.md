You are an expert Frontend Engineer and FinTech UI/UX Designer. Build a complete, interactive, single-file React application (using Tailwind CSS and `lucide-react` icons) for a FinTech hackathon project called **"DialectGuard"**.

### 1. Project Context & Competition Requirements
DialectGuard is an AI Personal Finance Assistant designed for elderly, dialect-speaking users (Cantonese/Hokkien/Manglish) and their adult children in Southeast Asia. 
Instead of making elderly users learn a complex banking app, the AI Agent operates inside WhatsApp, connected to a backend Open Banking Sandbox with a 3-Tier Permission & Anti-Scam Guardrail system.

To demonstrate the end-to-end flow to judges on a single computer screen, build a **"Split-Screen Command Center"** containing 3 vertical columns on top and 1 live terminal log panel at the bottom.

---

### 2. Layout Structure (100vh Full-Screen Dashboard)

#### Top Header Bar:
- Title: **DialectGuard — Multi-Dialect Financial Agent & Family Guardrail Sandbox**
- Quick Demo Scenario Trigger Buttons (for testing & recording the demo video):
  1. `[Scenario 1: Dialect Budget Check (Tier 1 - Read Only)]`
  2. `[Scenario 2: Real TNB Bill OCR & Auto-Pay (Tier 2 - Whitelist < RM50)]`
  3. `[Scenario 3: Scam Bill /RM800 Transfer Intercept (Tier 3 - Guardian Co-Sign)]`
  4. `[Reset Sandbox State]`

#### Main Grid (3 Columns):

**COLUMN 1 (Left): 👵 Parent's Mock WhatsApp ("Ah Por's Phone")**
- Styled like a mobile WhatsApp screen (dark green header `#075E54`, chat wallpaper background `#ECE5DD`).
- Header shows: "DialectGuard Finance Bot (Verified)" with an online status.
- Chat History area showing large-font, high-contrast messages (accessible for seniors):
  - Supports simulated **Voice Note bubbles** (with a play icon, waveform, dialect transcript e.g. *"🎤 [Cantonese/Manglish]: 帮我睇下呢张 Bil Api 几多钱？顺便交埋佢"*, and English translation subtitle underneath for international judges).
  - Supports **Uploaded Bill Image cards** (e.g., TNB Electricity Bill or Fake Lottery Scam Bill).
- Bottom Input Bar:
  - `[🎙️ Hold to Speak Dialect]` button, `[📷 Snap Bill Photo]` button, and a text input fallback.
  - When Scenario 2 requires Tier-2 confirmation, show a big green quick-reply button in the chat: `[🗣️ Confirm Pay RM 42.50 ("好，确认缴费")]`.

**COLUMN 2 (Middle): 🏦 Open Banking Sandbox & Permission Engine (Live Backend State)**
- Styled like a clean, modern FinTech dashboard (white/slate cards).
- **Account Overview Card:**
  - Account Holder: `Mdm. Tan (Ah Por) - Simulated Sandbox Acct #8829-102`
  - Live Balance: Starts at `RM 1,500.00` (dynamically deducts when bills are paid, or shows `RM 800.00 FROZEN` during Tier 3 scam interception).
- **Smart Budget Allocation (Deterministic Engine):**
  - Progress bars showing monthly income (`RM 1,500` from STR Government Aid + Child Allowance) split into:
    - Medical Reserve: `RM 300`
    - Utilities Whitelist Reserve: `RM 200` (updates when TNB bill is paid)
    - Daily Grocery Safe Limit: `RM 25.00 / day`
    - Insights Alert Badge: *"⚠️ Health Supplements overspent by RM 120 last week. Daily limit adjusted."*
- **Active Security Tier Indicator:**
  - Visually highlights which permission tier is currently active:
    - `Tier 1 (Green): Read-Only Query (Zero Risk)`
    - `Tier 2 (Blue): Whitelisted Utility Auto-Pay (< RM 50 + Voice Confirm)`
    - `Tier 3 (Red Pulse): High-Risk / Unverified Payee -> FROZEN (Maker-Checker Required)`
- **Recent Sandbox Transactions Table:**
  - Shows Transaction ID, Payee, Amount, Risk Tier, and Status (`SUCCESS`, `PENDING_VOICE`, `FROZEN_TIER_3`, `BLOCKED_BY_GUARDIAN`).

**COLUMN 3 (Right): 👨 Adult Child's Mock WhatsApp ("Guardian Phone - Son/Daughter")**
- Also styled like a mobile WhatsApp screen (`#075E54` header: "DialectGuard Family Alert Bot").
- Receives automated weekly spending insights on Ah Por's budget.
- **Tier 3 Maker-Checker Interactive Alert Card:**
  - When Scenario 3 is triggered, a high-priority warning bubble pops up in the Child's WhatsApp:
    - *"🚨 **URGENT TIER-3 INTERCEPTION**"*
    - *"Ah Por attempted to transfer **RM 800.00** to unverified account `Acct #9910-SCAM`."*
    - *"AI Risk Trigger: Scam keyword detected ('解冻奖金 / Unfreeze Prize') + Exceeds RM 50 limit."*
  - Inside this WhatsApp message bubble, render two working interactive buttons:
    - `[✅ Approve Transfer]` (Releases funds in Middle Sandbox & notifies Ah Por)
    - `[❌ Reject & Block Scammer]` (Cancels transaction, restores frozen funds, adds payee to blacklist, and sends a comforting dialect voice note to Ah Por's WhatsApp).

#### Bottom Panel (Full Width, ~180px height): 💻 Live Sandbox Operation Logs (Execution Evidence)
- Styled like a dark terminal (`bg-slate-950 text-emerald-400 font-mono text-xs`).
- Header: `LIVE SANDBOX EXECUTION LOGS (EXPORTABLE JSON/TEXT)` with a `[Copy Logs]` button.
- Every button click or scenario step appends realistic, timestamped technical log lines demonstrating the Neuro-Symbolic pipeline:
  - Example: `[16:05:12] [ASR-QLoRA] Model: malaysian-whisper-v3 | Dialect: Cantonese-MY | Text: "帮我交咗张电费单"`
  - Example: `[16:05:13] [OCR-Engine] Extracted Payee: "Tenaga Nasional Berhad (40092)" | Amount: MYR 42.50 | Z-Score: +0.4 (Normal)`
  - Example: `[16:05:14] [Guardrail-Gateway] Rule Check: Payee in Official_Whitelist == TRUE && Amount <= 50.00 -> Assigned TIER_2`
  - Example: `[16:05:18] [Sandbox-Ledger] TXN_88901 COMMITTED | Old_Bal: 1500.00 | New_Bal: 1457.50`

---

### 3. Interactive State Requirements (Must Work Out-of-the-Box)
Make all state transitions fully functional using React `useState`:
1. Clicking **[Scenario 1: Dialect Budget Check]** simulates Ah Por sending a dialect voice note asking how much she can spend on groceries today -> Logs update -> Bot replies in dialect with her `RM 25/day` breakdown and health supplement overspending warning.
2. Clicking **[Scenario 2: Real TNB Bill Auto-Pay]** simulates uploading a TNB bill (`RM 42.50`) -> Logs show OCR & Whitelist match -> Bot asks Ah Por to confirm -> Clicking `[Confirm Pay RM 42.50]` inside Ah Por's chat deducts `RM 42.50` from the Middle Bank Sandbox (`RM 1,500.00` -> `RM 1,457.50`), updates the transaction table to `SUCCESS`, and prints the sandbox transaction hash/ID in the terminal.
3. Clicking **[Scenario 3: Scam Transfer Intercept]** simulates Ah Por forwarding a fake lottery message asking to transfer `RM 800` -> Middle Sandbox flashes RED (`Tier 3 Triggered`), freezes `RM 800`, and logs the OWASP/Guardrail interception -> Child's WhatsApp immediately receives the alert bubble -> Clicking `[❌ Reject & Block Scammer]` in the Child's WhatsApp updates the transaction status to `BLOCKED_BY_GUARDIAN`, unfreezes the balance, and sends a safety confirmation message to both WhatsApp screens.

Write clean, well-structured, self-contained React code so it renders immediately in the preview canvas.