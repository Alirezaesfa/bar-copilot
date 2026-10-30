# Maplewave Telecom: billing adjustment demo data

**Everything here is synthetic.** Maplewave Telecom is a fictional company. All customers, people, contracts, invoices, outages and requests are made up for a demo of AI-assisted billing adjustment review. None of it comes from a real company.

## What's in the package

| Folder or file | Contents |
|---|---|
| `data/` | 9 tables as CSV files |
| `data_parquet/` | The same 9 tables as Parquet, with column types already set. Use these if a CSV import gives you trouble. |
| `policies/` | 7 policy documents in Markdown, for the policy search (RAG) |
| `DEMO_SCENARIOS.md` | The answer key for the planted demo cases. **Keep this file away from the agent** (see that file). |

## Time frame

- **Invoices:** 12 billing periods, from 2025-11 to 2026-10.
- **Request history:** 374 decided requests, submitted from 2025-12-01 to 2026-09-30.
- **Reviewer queue:** 12 requests with the status "Pending Review", submitted from 2026-10-13 to 2026-10-22.

## Tables

| File | Rows | What one row is |
|---|---|---|
| `services.csv` | 10 | A product Maplewave sells: its list price per unit and its availability target (SLA), if it has one |
| `promotions.csv` | 4 | A promotion and its rules |
| `employees.csv` | 19 | An account manager, reviewer, director or VP |
| `customers.csv` | 150 | A business customer |
| `contracts.csv` | 291 | One service contract for one customer |
| `invoices.csv` | 1,606 | One monthly invoice for one customer |
| `invoice_lines.csv` | 5,826 | One line on an invoice |
| `service_outages.csv` | 290 | One outage on one contract, with its trouble ticket |
| `adjustment_requests.csv` | 386 | One billing adjustment request: 374 decided, 12 pending |
| `review_decisions` | — | One AI or human decision record written against an adjustment request (Iceberg only, no CSV) |

### Key columns

**contracts.csv**
- `quantity`: units (sites, channels, seats, lines or access points).
- `unit_price_cad`: the contract price per unit.
- `discount_pct`: the contract discount.
- `net_mrc_cad`: the net monthly recurring charge, calculated as `unit_price × quantity × (1 − discount)`.
- `term_months`, `start_date`, `end_date`.
- `status`: Active or Terminated, plus `termination_date`.
- `promo_code`, `promo_monthly_credit_cad`, `promo_start_period`, `promo_end_period`: blank when the contract has no promotion.
- `sla_uptime_pct`: blank when the service has no SLA.
- `replaces_contract_id`: filled in when a new contract replaced a terminated one (an upgrade).

**invoice_lines.csv**
- `charge_type` is one of: Recurring, Discount, Promotional Credit, One-time, Usage, Early Termination Fee, Adjustment Credit.
- Approved credits from past requests appear on the following invoice as an "Adjustment Credit" line whose description ends with the request ID.

**service_outages.csv**
- `category` is one of: Network, Planned maintenance, Customer-side.
- `start_time`, `end_time`, `duration_minutes`.
- `affected_units`: how many of the contract's units were down.
- `ticket_id`: the trouble ticket ID.

**adjustment_requests.csv**
- `submitted_by` is the account manager's employee ID.
- `request_type` is one of: Billing Error, SLA Outage Credit, Promotion Not Applied, Duplicate Charge, Early Termination Fee Dispute.
- `billing_periods`: one period, or a range written as `2026-06 to 2026-10`.
- `requested_amount_cad`.
- `description`: free text, written the way an account manager would.
- `evidence_provided`.
- `missing_info_at_submission`: what was missing when the request came in.
- `status` is one of: Approved, Partially Approved, Rejected, Pending Review.
- `approved_amount_cad`, `decision_reason` and `policy_reference`: these show how each past case was decided.
- `reviewer_id`, `approval_level_required`, `approver_id`, `decision_date`.
- `review_minutes`: reviewer effort.
- `credit_status`, `credit_applied_invoice_id`.

**review_decisions** (Iceberg/Parquet — no CSV file, no id column)

Created with:
```sql
CREATE TABLE iceberg_data.maplewave.review_decisions (
    request_id              VARCHAR,
    decided_at              TIMESTAMP,
    decided_by              VARCHAR,
    role                    VARCHAR,
    ai_outcome              VARCHAR,
    ai_amount_cad           DOUBLE,
    final_outcome           VARCHAR,
    final_amount_cad        DOUBLE,
    approval_level_required VARCHAR,
    override_reason         VARCHAR,
    notes                   VARCHAR
) WITH (format = 'PARQUET')
```

- `request_id`: foreign key to `adjustment_requests`.
- `decided_at`: UTC timestamp of when the decision was recorded. Used as the ordering key (there is no id column); the latest row per `request_id` is `MAX(decided_at)`.
- `role`: the reviewer's role — `Reviewer`, `Director`, or `VP Finance Operations`.
- `ai_outcome` / `final_outcome`: one of `Approved`, `Partially Approved`, `Rejected`, `Returned for Information`, or `Recommended - awaiting Director`.
- `ai_amount_cad` / `final_amount_cad`: DOUBLE, amounts in CAD.
- `approval_level_required`: computed at write time by applying POL-001 §1 to `final_amount_cad`.
- `override_reason`: required when `final_outcome` differs from `ai_outcome`.
- `notes`: free text, may be empty.

## How the tables connect

```
customers ──< contracts ──< invoice_lines >── invoices >── customers
                 │   └──< service_outages
                 ├── services (service_code)
                 ├── promotions (promo_code)
                 └── contracts (replaces_contract_id)
adjustment_requests >── customers, contracts, invoices, employees (submitted_by, reviewer_id, approver_id)
review_decisions    >── adjustment_requests (request_id)
```

## Rules built into the data

The data follows the policies in `policies/` exactly:

- All amounts are in CAD, before tax.
- Invoices are issued on the first day of the billing period.
- Every decided request in the history can be verified from the records. A rate error shows up as a wrong unit price on the invoice line. A duplicate shows up as two identical recurring lines. A missing promotion shows up as a missing credit line. An SLA claim matches an outage with the right category, length and timing.
- The 5 request types are **illustrative**. They were chosen to show different kinds of reasoning, not copied from any real company.

An independent check script confirmed that:
- every decided request (374 of 374) reproduces from the raw records using only the policy rules;
- every invoice total equals the sum of its lines;
- no unexplained errors are hidden in the data.

## Past requests at a glance (synthetic)

| | Incomplete when submitted | Complete when submitted |
|---|---|---|
| Share of requests | 85% (318 of 374) | 15% (56 of 374) |
| Average review time | 49 minutes | 16 minutes |
| Average days to decision | 13.6 | 3.1 |

Across all 374 requests, the average review time is 44 minutes, and the total is about 276 reviewer hours. Outcomes:

| Request type | Approved | Partially approved | Rejected |
|---|---|---|---|
| Billing Error | 72 | 18 | 30 |
| SLA Outage Credit | 55 | 15 | 30 |
| Duplicate Charge | 45 | 0 | 15 |
| Promotion Not Applied | 24 | 6 | 14 |
| Early Termination Fee Dispute | 18 | 7 | 25 |

## Loading notes (for watsonx.data)

**CSV files**
- Header row, comma-separated, UTF-8.
- Text that contains commas is quoted.
- Dates are `YYYY-MM-DD`, and timestamps are `YYYY-MM-DD HH:MM:SS`.
- Booleans are `true` or `false`.
- An empty cell means no value.

**Parquet files**
- Same data, with dates, timestamps, integers and amounts already typed.

**Naming**
- Load each file as one table and keep the file name as the table name, for example `contracts`.

## Policies

| File | Covers |
|---|---|
| POL-000 | General billing adjustment policy: required information, evidence, lookback, goodwill, AI-assisted review |
| POL-001 | Approval authority matrix: over CAD 20,000 needs a Director, over CAD 100,000 also needs the VP |
| POL-101 | Billing error (rate mismatch) |
| POL-102 | SLA outage credit |
| POL-103 | Promotion not applied |
| POL-104 | Duplicate charge |
| POL-105 | Early termination fee |

Each section is numbered (§1, §2, and so on) so answers can cite it. Each section is under 130 words, so it fits in one chunk for an embedding model with a 512-token limit.
