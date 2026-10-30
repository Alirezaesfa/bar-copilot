# Plan: tools/bar_tools.py — Full Tool Suite

## Overview

Replace the single `get_customer_contracts` tool in `tools/bar_tools.py` with the full set of lookup, check, and write tools needed by the billing-adjustment agent. All tools live in one self-contained file. The `_get_cfg()` / `_connect()` helpers stay exactly as they are. Calculation math is isolated into small pure functions so they can be unit-tested without a database. Tests go in `tests/test_calculations.py` and cover only the worked examples from the policies. `agents/spike_agent.yaml` is updated to reference the two new lookup tools that replace the old one. `scripts/remove_all.py` is updated to remove every tool by name.

**Source of truth for all rules:** `knowledge/POL-000` through `POL-105`.  
**Source of truth for all column names:** `docs/DATA_DICTIONARY.md` plus the `review_decisions` table documented below.

## Confirmed Schema Details

### `review_decisions` table (Iceberg/Parquet, no id column)

```sql
CREATE TABLE iceberg_data.maplewave.review_decisions (
    request_id            VARCHAR,
    decided_at            TIMESTAMP,
    decided_by            VARCHAR,
    role                  VARCHAR,
    ai_outcome            VARCHAR,
    ai_amount_cad         DOUBLE,
    final_outcome         VARCHAR,
    final_amount_cad      DOUBLE,
    approval_level_required VARCHAR,
    override_reason       VARCHAR,
    notes                 VARCHAR
) WITH (format = 'PARQUET')
```

- No id column. "Latest" row for a request = `MAX(decided_at)`.
- Timestamp column is `decided_at` (not `decision_date`).
- Amount columns are `ai_amount_cad` and `final_amount_cad` (DOUBLE).

### `adjustment_requests` — request_id format

- IDs run `ADJ-10001` to `ADJ-10386` (5-digit suffix, zero-padded).
- Next id: `SELECT MAX(request_id) FROM adjustment_requests`, strip `ADJ-`, cast to int, add 1, format as `ADJ-{n:05d}`.
- No DB sequence; generate in Python.

### Valid outcome strings (POL-000 §8 + escalation rule)

Use **exactly** these strings everywhere (in code, comparisons, and DB values):

| String | When used |
|---|---|
| `Approved` | Full amount supported |
| `Partially Approved` | Partial amount supported |
| `Rejected` | Records do not support a credit |
| `Returned for Information` | POL-000 §3 items missing |
| `Recommended - awaiting Director` | final_amount_cad > 20,000 and role is Reviewer |

---

## Sub-task 1 — Pure calculation functions + pytest tests

**Status:** [ ] pending

### Intent
Extract every formula that the policy check tools need into small, pure Python functions that take only plain values and return a `Decimal`. These have no DB calls, so they are easy to test and the model never does math. Tests confirm the exact worked examples written in the policies.

### Expected Outcomes
- A block of pure functions at the top of `bar_tools.py` (below imports, above `_get_cfg`).
- `tests/test_calculations.py` passes with the four worked examples:
  - POL-101 → 540.00
  - POL-102 → 88.00 at 10% tier
  - POL-105 → 1,200.00 for 6 months
  - POL-001 `calc_approval_level` → Reviewer for 20,000.00, Director for 20,000.01, Director and VP for 100,000.01

### Todo List
1. Add `from decimal import Decimal, ROUND_HALF_UP` to imports.
2. Write `_cents(value) -> Decimal` — rounds a Decimal to 2 dp using ROUND_HALF_UP.
3. Write `calc_rate_error(invoiced_price, contract_price, quantity, discount_pct) -> Decimal` — implements POL-101 §3 formula for **one period**: `(invoiced_price − contract_price) × quantity × (1 − discount_pct/100)`. Returns the credit for a single month. The check tool accumulates across months.
4. Write `calc_sla_availability(outage_minutes, month_minutes) -> Decimal` — returns availability % as a Decimal (full precision, no rounding).
5. Write `calc_sla_credit_pct(shortfall) -> Decimal` — returns the credit percentage (Decimal 5, 10, 25, or 0) per POL-102 §4 tier table.
6. Write `calc_sla_credit(unit_price, affected_units, discount_pct, credit_pct) -> Decimal` — credit for the **affected units only**: `unit_price × affected_units × (1 − discount_pct/100) × credit_pct/100`; `_cents` at the end.
7. Write `calc_etf(net_mrc, remaining_months) -> Decimal` — 50% × net_mrc × remaining_months, `_cents` at the end; implements POL-105 §1.
8. Write `calc_approval_level(amount) -> str` — returns "Reviewer", "Director", or "Director and VP" per POL-001 §1 thresholds: ≤ 20,000.00 → "Reviewer"; 20,000.01–100,000.00 → "Director"; > 100,000.00 → "Director and VP".
9. Create `tests/test_calculations.py` with four test functions, one per policy worked example. Import only from `tools.bar_tools`. No fixtures, no DB. Run with `python -m pytest` from the project root so that `tools` is on the path.

### Relevant Context
- POL-101 §3 worked example: `(110−100) × 20 × 0.90 × 3 = 540.00`. The 3-period total is built by calling `calc_rate_error` three times and summing (or calling once and multiplying — same result when the price is constant, but the check tool must call per-period to handle varying prices month by month).
- POL-102 §7 worked example: `22.00 × 40 × 10% = 88.00` (shortfall 0.594 → 10% tier). The 40 here is `affected_units` from the outage record, not the contract quantity.
- POL-105 §1 worked example: `50% × 400.00 × 6 = 1,200.00`
- POL-001 §1 thresholds: ≤ 20,000.00 → "Reviewer"; 20,000.01–100,000.00 → "Director"; > 100,000.00 → "Director and VP" (both approve per POL-001 §1).
- Use `Decimal` for all monetary input values in tests (pass as strings to `Decimal("...")`).
- `discount_pct` is stored as a percentage integer/decimal in the DB (e.g. 10 means 10%); functions must divide by 100.

---

## Sub-task 2 — Lookup tools: find_customers + get_contracts

**Status:** [ ] pending

### Intent
Replace `get_customer_contracts` with two focused tools. `find_customers` searches customers only (max 5 rows) so the agent can confirm which customer the user means before pulling contracts. `get_contracts` fetches all contracts for a confirmed customer_id.

### Expected Outcomes
- `get_customer_contracts` is removed from the file.
- `find_customers(name_text: str) -> List[Dict]` is registered as a `@tool`.
- `get_contracts(customer_id: str) -> List[Dict]` is registered as a `@tool`.
- `agents/spike_agent.yaml` tools list updated to `[find_customers, get_contracts]`.

### Todo List
1. Delete the `get_customer_contracts` function.
2. Add `find_customers(name_text: str)`:
   - SQL: `SELECT customer_id, customer_name, city, province, account_manager_id FROM customers WHERE LOWER(customer_name) LIKE ? LIMIT 5`
   - Returns list of dicts; short columns only.
   - Docstring explains the 5-result cap.
3. Add `get_contracts(customer_id: str)`:
   - SQL: join `contracts` to `services` on `service_code`; return `contract_id`, `service_code`, `service_name`, `quantity`, `unit_price_cad`, `discount_pct`, `net_mrc_cad`, `status`, `start_date`, `end_date`, `sla_uptime_pct`, `promo_code`.
   - Filter: `contracts.customer_id = ?`, order by `contract_id`.
   - Returns list of dicts.
4. Update `agents/spike_agent.yaml`: replace `- get_customer_contracts` with `- find_customers` and `- get_contracts`.

### Relevant Context
- Existing tool pattern to follow: `@tool(expected_credentials=[...])`, `_get_cfg()`, `_connect()`, cursor pattern in the current file.
- `customers` columns: `customer_id`, `customer_name`, `city`, `province`, `account_manager_id` (plus others not needed here).
- `contracts` columns per data dictionary: `contract_id`, `customer_id`, `service_code`, `quantity`, `unit_price_cad`, `discount_pct`, `net_mrc_cad`, `status`, `start_date`, `end_date`, `sla_uptime_pct`, `promo_code`, `promo_monthly_credit_cad`, `promo_start_period`, `promo_end_period`, `term_months`, `termination_date`, `replaces_contract_id`.
- `services` columns: `service_code`, `service_name` (plus `list_price_cad`, `sla_uptime_pct`).
- Presto type rules (apply in every tool): convert Decimal amounts to `float` before passing as query parameters; pass Python `datetime.date` / `datetime.datetime` objects for DATE / TIMESTAMP columns (not strings); parse date/timestamp strings that come back from queries using `datetime.date.fromisoformat` / `datetime.datetime.fromisoformat`.

---

## Sub-task 3 — Lookup tools: get_invoice_lines, get_outages, get_request, list_queue, get_past_decisions

**Status:** [ ] pending

### Intent
Add the remaining read-only lookup tools needed by both agents to pull facts from the database.

### Expected Outcomes
Five new `@tool` functions added to `bar_tools.py`, all read-only.

### Todo List
1. Add `get_invoice_lines(contract_id: str, from_period: str, to_period: str) -> List[Dict]`:
   - Join `invoice_lines` → `invoices` on `invoice_id`.
   - Columns: `invoice_id`, `billing_period`, `line_id`, `charge_type`, `description`, `quantity`, `unit_price_cad`, `amount_cad`.
   - Filter: `contract_id = ?` AND `billing_period BETWEEN ? AND ?`.
   - Order by `billing_period`, `line_id`.
2. Add `get_outages(contract_id: str, from_date: str, to_date: str) -> List[Dict]`:
   - Table: `service_outages`.
   - Columns: `outage_id`, `ticket_id`, `category`, `start_time`, `end_time`, `duration_minutes`, `affected_units`.
   - Filter: `contract_id = ?` AND `start_time >= ?` AND `end_time <= ?`.
   - Order by `start_time`.
3. Add `get_request(request_id: str) -> Dict`:
   - Table: `adjustment_requests`.
   - Returns all columns for a single row (or empty dict if not found).
   - Filter: `request_id = ?`.
4. Add `list_queue() -> List[Dict]`:
   - Fetch all rows from `adjustment_requests` where `status = 'Pending Review'`.
   - Left join `review_decisions` using a subquery on `MAX(decided_at)` per `request_id` to get the latest decision row.
   - Columns to return: `request_id`, `customer_id`, `contract_id`, `request_type`, `billing_periods`, `requested_amount_cad`, `submitted_date`, `submitted_by`, plus from the latest review_decisions row (if exists): `ai_outcome`, `ai_amount_cad`, `final_outcome`, `final_amount_cad`, `decided_by`.
   - Order by `submitted_date`.
5. Add `get_past_decisions(request_type: str) -> List[Dict]`:
   - SQL: last 3 decided requests of the given type where `status IN ('Approved', 'Partially Approved', 'Rejected')`, ordered by `decision_date DESC`, limit 3.
   - Columns: `request_id`, `request_type`, `approved_amount_cad`, `decision_reason`, `policy_reference`, `decision_date`.

### Relevant Context
- `invoice_lines` columns: `line_id`, `invoice_id`, `contract_id`, `charge_type`, `description`, `quantity`, `unit_price_cad`, `amount_cad`.
- `invoices` columns: `invoice_id`, `customer_id`, `billing_period`, `invoice_date`, `total_amount_cad`.
- `service_outages` columns: `outage_id`, `contract_id`, `ticket_id`, `category`, `start_time`, `end_time`, `duration_minutes`, `affected_units`.
- `adjustment_requests` key columns: `request_id`, `customer_id`, `contract_id`, `request_type`, `billing_periods`, `requested_amount_cad`, `submitted_date`, `submitted_by`, `status`, `decision_reason`, `policy_reference`, `approved_amount_cad`, `decision_date`.
- `review_decisions` columns: `request_id`, `decided_at` (TIMESTAMP), `decided_by`, `role`, `ai_outcome`, `ai_amount_cad` (DOUBLE), `final_outcome`, `final_amount_cad` (DOUBLE), `approval_level_required`, `override_reason`, `notes`. No id column; latest row = MAX(decided_at).

---

## Sub-task 4 — Check tools (policy eligibility tools)

**Status:** [ ] pending

### Intent
Add seven read-only check tools. Each one runs the appropriate SQL lookups, calls the pure calculation functions from Sub-task 1, and returns a structured dict with: `eligible` (bool), `amount` (Decimal as string), `calculation` (one-line string), `evidence_ids` (list), `policy_sections` (list), and any policy-specific facts described below.

### Expected Outcomes
Seven new `@tool` functions added, all read-only, no side-effects.

### Todo List

#### 4a. `check_rate_error(contract_id, from_period, to_period, as_of_date)`
- Fetch invoice lines for the range (using `get_invoice_lines` SQL logic inline).
- Fetch contract `unit_price_cad`, `discount_pct`, `quantity`.
- For each billing period in the range that falls within the 12-month lookback from `as_of_date` (POL-000 §5), check if any Recurring line's `unit_price_cad` differs from the contract price.
- **Month-by-month accumulation:** call `calc_rate_error(invoiced_price, contract_price, quantity, discount_pct)` once per wrong period and sum the results — this handles the case where the invoiced price varies from month to month.
- `periods_wrong` is the list of billing periods where the price was wrong (not just a count).
- **`latest_invoice_still_wrong`:** fetch the `unit_price_cad` from the most recent Recurring invoice line for this contract across **all** invoices (not just the requested range), and compare it to the contract price (POL-101 §5).
- Return fields: `eligible`, `amount`, `calculation`, `evidence_ids` (invoice_ids of wrong periods), `policy_sections` (["POL-101 §2", "POL-101 §3"]), `periods_checked`, `periods_wrong` (list), `latest_invoice_still_wrong`.

#### 4b. `check_sla_credit(contract_id, month, as_of_date)`
- Fetch contract `service_code`, `sla_uptime_pct`, `unit_price_cad`, `discount_pct`, `quantity`.
- Check if service has an SLA (`sla_uptime_pct` is not null/blank) → set `sla_eligible_service`. If not eligible, return early with `eligible: false`.
- Fetch outages for the month filtered to `category = 'Network'` only (POL-102 §3 — planned maintenance and customer-side outages do not qualify).
- Sum `duration_minutes` across all Network outages for the contract in that month (POL-102 §2).
- Take `affected_units` from the outage record (the number of units that were actually down). If multiple outages, use the maximum `affected_units` across them (same month, same contract).
- Compute total minutes in the month: `calendar.monthrange(year, month)[1] × 1440`.
- Call `calc_sla_availability(total_outage_minutes, month_minutes)` and `calc_sla_credit_pct(shortfall)`.
- Check 60-day limit: days from the latest `end_time` among Network outages in the month to `as_of_date` (POL-102 §5). `within_60_days = days_since_outage_end <= 60`.
- Call `calc_sla_credit(unit_price_cad, affected_units, discount_pct, credit_pct)` — **affected_units only, not contract quantity** (POL-102 §4: "units that were not affected get no credit").
- Return fields: `eligible`, `amount`, `calculation`, `evidence_ids` (ticket_ids), `policy_sections`, `sla_eligible_service`, `availability_pct`, `shortfall`, `credit_tier_pct`, `affected_units`, `days_since_outage_end`, `within_60_days`.

#### 4c. `check_promotion(contract_id, from_period, to_period)`
- Fetch contract `promo_code`, `promo_monthly_credit_cad`, `promo_start_period`, `promo_end_period`.
- If no promo_code → not eligible.
- For each billing period in range that falls within the promo period, check invoice lines for a `Promotional Credit` line for this contract; note missing or short-credited months.
- Credit = `promo_monthly_credit_cad × missing_months` + differences for short-credited months (POL-103 §3).
- Return fields: `eligible`, `amount`, `calculation`, `evidence_ids` (invoice_ids), `policy_sections` (["POL-103 §2", "POL-103 §3"]), `promo_code`, `promo_period`, `periods_missing`.

#### 4d. `check_duplicates(contract_id, period)`
- Fetch invoice lines for `contract_id` in `period` where `charge_type = 'Recurring'`.
- Group by `(charge_type, description, unit_price_cad, amount_cad)`; any group with count > 1 is a duplicate set.
- Credit = sum of extra copies' `amount_cad`, minus any duplicated discount lines (POL-104 §3).
- Return fields: `eligible`, `amount`, `calculation`, `evidence_ids` (invoice_ids, line_ids), `policy_sections` (["POL-104 §1", "POL-104 §3"]), `duplicate_sets` (brief list of duplicated descriptions and counts).

#### 4e. `check_etf(contract_id, request_text)`
- Fetch contract: `net_mrc_cad`, `termination_date`, `end_date`, `status`, `term_months`, `start_date`, `replaces_contract_id`, `sla_uptime_pct`.
- Compute `remaining_months` = full months from the month after `termination_date` to `end_date` inclusive (POL-105 §1).
- Fetch billed ETF from invoice lines (`charge_type = 'Early Termination Fee'` on the termination-month invoice).
- Call `calc_etf(net_mrc_cad, remaining_months)` → expected fee.
- **Waiver checks:**
  - **(a) Chronic failure (POL-105 §2a):** only if `sla_uptime_pct` is not null (non-SLA services → `waiver_a = False` immediately). Fetch outages for the 6 billing periods up to and including the termination month; filter to `category = 'Network'` only (POL-102 §3 — planned maintenance and customer-side do not count). For each of those 6 months, sum Network `duration_minutes`, compute availability via `calc_sla_availability`, compare to `sla_uptime_pct`; count months where availability < target. `waiver_a = count >= 3`.
  - **(b) Relocation (POL-105 §2b):** search `request_text` for an `SC-` number (regex `SC-\d+`); waiver_b = found.
  - **(c) Upgrade (POL-105 §2c):** look up `replaces_contract_id` on other contracts or check if any contract has `replaces_contract_id = contract_id`; check if it was signed within 30 days of `termination_date`, term ≥ original, net_mrc ≥ original; waiver_c = qualifies.
- If any waiver → eligible for full waiver (`amount = billed_etf`); else if `billed_etf != expected_etf` → eligible for the difference (`amount = billed_etf − expected_etf`); else not eligible.
- Return fields: `eligible`, `amount`, `calculation`, `evidence_ids` (invoice_id, ticket_ids, new_contract_id if applicable), `policy_sections`, `billed_etf`, `expected_etf`, `waiver_a` (bool + `months_below_sla` count), `waiver_b` (bool + `sc_number`), `waiver_c` (bool + `new_contract_id`).

#### 4f. `check_completeness(request_id)`
- Fetch the request from `adjustment_requests`.
- Check each POL-000 §3 required item against the request columns:
  1. `customer_id` present (not null/empty)
  2. `contract_id` present
  3. `request_type` present
  4. `billing_periods` present
  5. `invoice_id` column present (not null/empty) — this is the invoice ID column, not `evidence_provided`
  6. `requested_amount_cad` present and > 0
  7. Type-specific evidence: only `SLA Outage Credit` requests need a `TKT-` ticket ID. Search `evidence_provided` **and** `description` for a token matching `TKT-\d+`. All other request types pass item 7 automatically.
- `evidence_provided` is free text (e.g. "Invoice copy" or blank) and is **not** checked for invoice IDs.
- Return fields: `complete` (bool), `missing_items` (list of item numbers with short descriptions), `policy_sections` (["POL-000 §3"]).

#### 4g. `approval_level(amount)`
- Tool wrapper: calls `calc_approval_level(amount)` (the pure calc function).
- Takes `amount: float` (convert to `Decimal(str(amount))` inside), returns `{"level": "Reviewer"|"Director"|"Director and VP", "policy_section": "POL-001 §1"}`.

### Relevant Context
- All check tools return `amount` as a string representation of a Decimal (e.g. `"540.00"`) so JSON serialisation is clean.
- `as_of_date` drives both the 12-month lookback (POL-000 §5) and the 60-day limit (POL-102 §5).
- Lookback: only billing periods where `billing_period >= (as_of_date minus 12 months)` in YYYY-MM format.
- Month minutes: `calendar.monthrange(year, month)[1] * 1440` in Python.
- Keep tool return dicts small — only the fields listed. No raw SQL dumps.
- Presto type rules (repeated here for emphasis): amounts to `float` before binding as parameters; `datetime.date` / `datetime.datetime` objects for DATE / TIMESTAMP parameters; parse returned date strings with `fromisoformat`.

---

## Sub-task 5 — Write tools: submit_request + record_decision

**Status:** [ ] pending

### Intent
Add the two state-changing tools. Both write to Iceberg tables via Presto INSERT. Rules for `record_decision` are enforced in Python before the INSERT.

### Expected Outcomes
Two new `@tool` functions that write to `adjustment_requests` and `review_decisions` respectively.

### Todo List

1. Add `submit_request(customer_id, contract_id, invoice_id, request_type, billing_periods, requested_amount_cad, description, evidence_provided, submitted_by) -> Dict`:
   - `invoice_id` is a required parameter (POL-000 §3 item 5); it is stored in the `invoice_id` column of `adjustment_requests`.
   - If `submitted_by` is empty or None, look up the customer's `account_manager_id` from the `customers` table and use that.
   - Generate next `request_id`: `SELECT MAX(request_id) FROM adjustment_requests`, strip `ADJ-`, cast to int, add 1, format as `ADJ-{n:05d}`.
   - Set `submitted_date` = `datetime.date.today()` (Python date object; pass as DATE parameter, not a string).
   - INSERT row with `status = 'Pending Review'`.
   - Return `{"request_id": new_id, "submitted_date": submitted_date.isoformat()}`.

2. Add `record_decision(request_id, decided_by, ai_outcome, ai_amount_cad, final_outcome, final_amount_cad, override_reason, notes) -> Dict`:
   - **No `role` parameter.** Look up `decided_by` in the `employees` table by matching `employee_id` OR `full_name` (case-insensitive). If not found, raise `ValueError`. Read `role` from the employee record. If `role` is not `"Reviewer"` or `"Director"`, raise `ValueError`.
   - **Rule 2 check — before Rule 1 modifies anything:** capture the caller's original `final_outcome` and `final_amount_cad`. If `final_outcome != ai_outcome` **or** `float(final_amount_cad) != float(ai_amount_cad)`, that is an override. If it is an override and `override_reason` is empty, raise `ValueError` immediately.
   - **Rule 1 — Director escalation:** after the Rule 2 check, if `Decimal(str(final_amount_cad)) > Decimal("20000.00")` and `role == "Reviewer"`, set `saved_outcome = "Recommended - awaiting Director"`. Otherwise `saved_outcome = final_outcome`.
   - **Rule 3 — Director authority:** if `role == "Director"`, query the latest `final_outcome` from `review_decisions` for this `request_id` (ORDER BY `decided_at` DESC LIMIT 1); proceed only if it is `"Recommended - awaiting Director"`, otherwise raise `ValueError`.
   - INSERT into `review_decisions` with `decided_at = datetime.datetime.utcnow()` (TIMESTAMP). Set `approval_level_required = calc_approval_level(final_amount_cad)`. Pass `notes` through (may be empty string).
   - Return `{"request_id": request_id, "final_outcome": saved_outcome, "decided_at": decided_at.isoformat()}`.

### Relevant Context
- `review_decisions` exact columns: `request_id`, `decided_at` (TIMESTAMP), `decided_by`, `role`, `ai_outcome`, `ai_amount_cad` (DOUBLE), `final_outcome`, `final_amount_cad` (DOUBLE), `approval_level_required`, `override_reason`, `notes`. No id column.
- `employees` columns include at minimum: `employee_id`, `full_name`, `role`. Look up by either field, case-insensitive.
- Iceberg via Presto supports `INSERT INTO` but not `RETURNING`; the `request_id` is generated in Python before the INSERT.
- Rule 2 is intentionally checked **before** Rule 1 changes `final_outcome`, so the comparison is against what the person actually decided, not the escalated label.
- A different amount (even with the same outcome string) counts as an override and requires `override_reason`.
- Valid `final_outcome` strings stored in DB: `Approved`, `Partially Approved`, `Rejected`, `Returned for Information`, `Recommended - awaiting Director`.
- Presto type rule: pass `ai_amount_cad` and `final_amount_cad` as `float` to the INSERT parameter list.

---

## Sub-task 6 — Update remove_all.py

**Status:** [ ] pending

### Intent
Replace the single `get_customer_contracts` removal command with removal commands for every tool defined in `bar_tools.py`.

### Expected Outcomes
`scripts/remove_all.py` lists all 16 tools by name in the removal block (count is unchanged; `approval_level` is still 1 tool, `calc_approval_level` is a private function and not deployed separately).

### Todo List
Replace the one `run([ORC, "tools", "remove", "-n", "get_customer_contracts"], ...)` line with individual `run(...)` calls for each tool:

**Lookup tools (7):**
- `find_customers`
- `get_contracts`
- `get_invoice_lines`
- `get_outages`
- `get_request`
- `list_queue`
- `get_past_decisions`

**Check tools (7):**
- `check_rate_error`
- `check_sla_credit`
- `check_promotion`
- `check_duplicates`
- `check_etf`
- `check_completeness`
- `approval_level`

**Write tools (2):**
- `submit_request`
- `record_decision`

### Relevant Context
- Existing pattern: `run([ORC, "tools", "remove", "-n", "<name>"], "Remove tool <name>")`.
- Keep the `exit-on-failure = False` (soft) behaviour — the resource may not exist.
- Order doesn't matter for tool removal, but keep it grouped for readability.

---

## Resolved Questions

All questions confirmed before implementation begins:

1. **`review_decisions` schema** — No id column. Timestamp is `decided_at`. Full schema documented in "Confirmed Schema Details" above.
2. **ADJ- format** — 5-digit suffix (`ADJ-10001` → `ADJ-10386`). Next = `MAX` suffix + 1, formatted `ADJ-{n:05d}`. No DB sequence.
3. **Multiple outages in a month** — Sum all `duration_minutes` for Network outages on the same contract in the same month (POL-102 §2). Confirmed.
4. **ETF waiver (a) non-SLA** — If `sla_uptime_pct` is NULL, `waiver_a = False` always. Confirmed.
5. **`list_queue` latest row** — No id column; use `MAX(decided_at)` to identify the latest `review_decisions` row per request.
6. **`get_past_decisions` "decided"** — `status IN ('Approved', 'Partially Approved', 'Rejected')`. Returned for Information is excluded. Confirmed.
7. **Two agents** — No second agent in this task. Only update `spike_agent.yaml` with the new lookup tool names. Second agent is a separate future step.
