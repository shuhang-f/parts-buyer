# Parts Buyer 🔧

> **"It shops like your best buyer and documents it like your auditor. The model never holds the checkbook."**

**Live Demo:** https://parts-buyer-production.up.railway.app/  
**Built at:** Personal Agents Hackathon (AWS Loft SF, Nov 20, 2026)  
**Team:** Shuhang Feng & Michael Cain

---

## Overview

**Parts Buyer** is the shop owner's personal purchasing agent for an equipment repair shop. It monitors parts inventory against open repair jobs, shops approved suppliers when stock runs short, rechecks live supplier stock and pricing before buying, enforces the owner's spending limits, and logs a plain-English audit trail ("receipts") for every decision.

### Core Principle: The Model Never Holds the Checkbook
All money and compliance rules (**R1–R7**) are enforced deterministically in Python (`rules.py`). The AI agent (`agent.py` + `tools.py`) inspects context, invokes tools, and writes human-readable explanations—but it cannot bypass supplier approval rules, skip a live stock recheck, exceed a spending limit without owner sign-off, or place duplicate orders.

---

## Hard Rules Enforced (`rules.py`)

| Rule | Guardrail | What It Prevents |
|---|---|---|
| **R1** | **Available stock math** (`on_hand - reserved < reorder_point`) | Missing parts reserved for upcoming repair jobs (`J1`–`J4`) |
| **R2** | **Order quantity & MOQ / bulk tier optimization** | Ordering below supplier minimum batch size or missing cheaper bulk tiers |
| **R3** | **Approved suppliers only** | Buying critical parts from unapproved brokers |
| **R4** | **Job deadline & lead-time verification** | Ordering cheap parts that arrive after a scheduled repair job's due date |
| **R5** | **Mandatory live stock & price recheck before ordering** | Ordering against stale catalog stock (e.g., `P01` catalog says 4, live check says 0) |
| **R6** | **Spending limits & human-in-the-loop approvals** | Auto-committing orders over the owner's limit (routes to **Approvals** as `pending_approval`) |
| **R7** | **Daily idempotency (`YYYY-MM-DD:PartID`) & crash recovery** | Double-ordering on retries or after a mid-run crash (`Crash test` mode) |

---

## Sponsor Integrations

Each sponsor integration lives in its own module, is controlled via environment flags, degrades gracefully on network errors (`sponsor_error` logging + fallback), and appears directly in the UI:

### 1. Amplitude + Statsig (`config_flags.py`) — Live Rules
- Reads the shop owner's live spending limit from Statsig Dynamic Config **`parts_buyer_rules`** (`spend_limit`) via `statsig-python-core`.
- Sidebar **Live rules (Statsig)** widget polls every 2 seconds so changing `spend_limit` in Statsig (e.g., `$1,000` → `$3,000`) immediately flips high-value orders (`P06 Light source lamp module`, `$2,395`) between `pending_approval` and `placed` with zero code changes or redeploys.

### 2. Cognee Cloud (`memory.py`) — Agent Memory
- Syncs shop purchasing policy (`docs/policy.md`) and stores run-over-run supplier lessons (`/api/v1/remember`, `/api/v1/remember/entry`, `/api/v1/recall`, `/api/v1/search`).
- Before shopping each part, recalls relevant policy clauses and prior run outcomes (such as stale stock or rejected offers) and attaches them to the audit trail (`step = "recalled"`).
- Includes an interactive **🧠 Inspect Cognee** dialog in the sidebar to check dataset status, sync `policy.md`, and run live memory recall queries.

### 3. Firedrill (`simulations.py`, `firedrill.recipe.json`, `scripts/firedrill_adapter.py`) — Simulation & Hard-Rule Scorecard
- **7 isolated stress-test simulation drills** run in throwaway SQLite databases (never touching live data) against both the **Rules engine** and **Live AI Agent**:
  1. `stale_stock_p01` — Stale catalog stock caught on live recheck (`R5`, `R3`)
  2. `landed_cost_p02` — True landed cost (unit price × qty + shipping) vs. unit price (`R2`, `R3`)
  3. `deadline_p04` — Urgent job deadline rejects slow shipping (`R4`, `R5`)
  4. `unapproved_and_cap_p06` — Unapproved broker blocked & spending cap triggers owner approval (`R3`, `R6`)
  5. `live_price_spike_p01` — Live price surge caught during recheck updates buy price (`R5`, `R6`)
  6. `idempotent_rerun` — Back-to-back rerun produces zero duplicate orders (`R7`)
  7. `crash_recovery` — Mid-run crash after 2 orders recovers cleanly without duplicates (`R7`, `R5`)
- Achieves **7/7 scenarios passed (100%)** and **0 hard-rule violations**.

---

## Repository Structure

```text
├── app.py                     # Streamlit UI (Inventory, Approvals, Orders, Pricing, Simulations, Audit trail)
├── rules.py                   # Deterministic purchasing & compliance rules (R1–R7)
├── agent.py                   # LLM tool-calling agent (Gemini / OpenRouter) with live event streaming
├── tools.py                   # Agent tools wrapping rules.py & audit.py
├── suppliers.py               # Supplier catalog & live stock/price recheck adapter
├── memory.py                  # Cognee Cloud memory integration (remember / recall / inspect)
├── config_flags.py            # Statsig live spending limit integration
├── simulations.py             # Firedrill 7-scenario stress-test suite & hard-rule verifier
├── firedrill.recipe.json      # Firedrill CLI / MCP simulation recipe
├── scripts/
│   └── firedrill_adapter.py   # Firedrill CLI target adapter
├── check_answer_key.py        # Deterministic answer-key acceptance test verifier
├── db.py                      # SQLite schema & demo data seeder
├── audit.py                   # Append-only audit log ("receipts")
├── data/                      # Seed CSVs (parts, suppliers, offers, jobs)
└── docs/
    ├── SPEC.md                # Full architecture & acceptance specification
    └── policy.md              # Shop purchasing policy ingested into Cognee memory
```

---

## Local Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Seed SQLite database & run answer-key verification
python db.py
python check_answer_key.py --all

# Launch Streamlit app
streamlit run app.py
```
