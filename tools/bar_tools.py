"""Maplewave billing-adjustment tools — Presto/Iceberg backed."""

import os
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import tool

APP_ID = "maplewave_data"

# ---------------------------------------------------------------------------
# Policy quotes — word-for-word from knowledge/*.txt, keyed by section ref.
# ---------------------------------------------------------------------------

POLICY_QUOTES: Dict[str, str] = {
    "POL-000 §3": (
        "A request missing any of these items is returned to the submitter with "
        'the outcome "Returned for Information". '
        "The review time target in POL-001 §4 starts only when the request is complete."
    ),
    "POL-101 §2": (
        "A credit is due when the unit price on the invoice is higher than the "
        "unit price in the contract, and no signed contract amendment changed the price. "
        "Prices can only change through a signed amendment."
    ),
    "POL-101 §3": (
        "Credit = (invoiced unit price − contract unit price) × quantity × "
        "(1 − contract discount %) × number of affected billing periods. "
        "Only billing periods within the 12-month lookback count (POL-000 §5). "
        "If the invoiced price was wrong in some months and correct in others, "
        "count only the months where it was wrong."
    ),
    "POL-102 §1": (
        "SLA credits apply only to services that have an availability target in "
        "the contract (the SLA uptime %)."
    ),
    "POL-102 §4": (
        "The credit is capped at 25% of the affected units' net MRC per month. "
        "Units that were not affected get no credit."
    ),
    "POL-102 §5": (
        "The request must be submitted within 60 days after the outage ended. "
        "Later requests are not eligible, even if the outage qualified."
    ),
    "POL-103 §2": (
        "A credit is due when all three conditions are met:"
    ),
    "POL-103 §3": (
        "Credit = monthly promotional credit on the contract × number of billing "
        "periods where the credit is missing. "
        "If the credit was applied but was too small, credit the difference for "
        "that month instead."
    ),
    "POL-104 §1": (
        "A duplicate charge is a charge that appears more than once for the same "
        "contract, with the same charge type and description, the same billing "
        "period and the same amount. The copies can be on one invoice, or on "
        "different invoices for the same billing period."
    ),
    "POL-104 §3": (
        "Credit = the amount of each extra copy of the charge. If the matching "
        "discount line was duplicated too, subtract it, so that the credit is "
        "the net amount."
    ),
    "POL-105 §1": (
        "When a business customer ends a contract before its end date, an early "
        "termination fee (ETF) applies:"
    ),
    "POL-105 §2": (
        "The ETF is waived in full only if at least one of these conditions is met:"
    ),
    "POL-105 §2(a)": (
        "in the 6 billing periods up to and including the termination month, "
        "the contract had at least 3 months where measured availability was "
        "below the SLA target (POL-102 §2 to §4)."
    ),
    "POL-105 §4": (
        "Exceptions can only be made as goodwill under POL-000 §7."
    ),
}

# ---------------------------------------------------------------------------
# Pure calculation functions (no DB access — testable in isolation)
# ---------------------------------------------------------------------------

def _cents(value: Decimal) -> Decimal:
    """Round a Decimal to 2 decimal places using ROUND_HALF_UP."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calc_rate_error(
    invoiced_price: Decimal,
    contract_price: Decimal,
    quantity: Decimal,
    discount_pct: Decimal,
) -> Decimal:
    """Return the billing-error credit for one period per POL-101 §3."""
    return _cents(
        (invoiced_price - contract_price)
        * quantity
        * (1 - discount_pct / 100)
    )


def calc_sla_availability(outage_minutes: Decimal, month_minutes: Decimal) -> Decimal:
    """Return availability percentage as a full-precision Decimal (no rounding)."""
    return (1 - outage_minutes / month_minutes) * 100


def calc_sla_credit_pct(shortfall: Decimal) -> Decimal:
    """Return SLA credit percentage (5, 10, 25, or 0) per POL-102 §4 tier table.

    POL-102 §4 uses "more than": a shortfall of exactly 0.50 → 5 %, exactly 1.00 → 10 %.
    """
    if shortfall > Decimal("1"):
        return Decimal("25")
    elif shortfall > Decimal("0.5"):
        return Decimal("10")
    elif shortfall > Decimal("0"):
        return Decimal("5")
    return Decimal("0")


def calc_sla_credit(
    unit_price: Decimal,
    affected_units: Decimal,
    discount_pct: Decimal,
    credit_pct: Decimal,
) -> Decimal:
    """Return SLA credit for affected units only per POL-102 §4."""
    return _cents(
        unit_price * affected_units * (1 - discount_pct / 100) * credit_pct / 100
    )


def calc_etf(net_mrc: Decimal, remaining_months: Decimal) -> Decimal:
    """Return early termination fee (50% × net_mrc × remaining_months) per POL-105 §1."""
    return _cents(Decimal("0.50") * net_mrc * remaining_months)


def calc_approval_level(amount: Decimal) -> str:
    """Return approval level string per POL-001 §1 thresholds."""
    if amount > Decimal("100000.00"):
        return "Director and VP"
    elif amount > Decimal("20000.00"):
        return "Director"
    return "Reviewer"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_cfg() -> Dict[str, str]:
    """Return connection settings from Orchestrate or fall back to .env."""
    try:
        from ibm_watsonx_orchestrate.run import connections as wxo_conn
        kv = wxo_conn.key_value(APP_ID)
        return {k: kv.get(k, "") for k in
                ["WXD_USER", "WXD_API_KEY", "PRESTO_HOST",
                 "PRESTO_PORT", "PRESTO_CATALOG", "PRESTO_SCHEMA"]}
    except (Exception, SystemExit):
        pass
    load_dotenv(override=True)
    return {k: os.environ[k] for k in
            ["WXD_USER", "WXD_API_KEY", "PRESTO_HOST",
             "PRESTO_PORT", "PRESTO_CATALOG", "PRESTO_SCHEMA"]}


def _connect(cfg: Dict[str, str]):
    """Open and return a Presto DBAPI connection using LDAP/basic auth."""
    import prestodb
    user = cfg["WXD_USER"]
    ldap_user = user if user.startswith("ibmlhapikey_") else f"ibmlhapikey_{user}"
    return prestodb.dbapi.connect(
        host=cfg["PRESTO_HOST"],
        port=int(cfg["PRESTO_PORT"]),
        user=ldap_user,
        catalog=cfg["PRESTO_CATALOG"],
        schema=cfg["PRESTO_SCHEMA"],
        http_scheme="https",
        auth=prestodb.auth.BasicAuthentication(ldap_user, cfg["WXD_API_KEY"]),
    )


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------

@tool
def get_today() -> Dict:
    """Return today's date so the agent knows the current date.

    Returns:
        Dict with keys today (YYYY-MM-DD), year (int), month (int).
    """
    import datetime
    today = datetime.date.today()
    return {"today": today.isoformat(), "year": today.year, "month": today.month}


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def find_customers(name_text: str) -> List[Dict]:
    """Search customers by partial name match.

    Args:
        name_text: Partial or full customer name to search for (case-insensitive).

    Returns:
        List of up to 5 matching customer dicts with customer_id, customer_name, city,
        province, account_manager_id.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT customer_id, customer_name, city, province, account_manager_id
        FROM {cat}.{sch}.customers
        WHERE LOWER(customer_name) LIKE ?
        LIMIT 5
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [f"%{name_text.lower()}%"])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_contracts(customer_id: str) -> List[Dict]:
    """Return all contracts for a customer, joined with service names.

    Args:
        customer_id: The customer ID from find_customers (e.g. "CUS-00042").

    Returns:
        List of contract dicts with contract_id, service_code, service_name,
        quantity, unit_price_cad, discount_pct, net_mrc_cad, status, start_date,
        end_date, sla_uptime_pct, promo_code.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT
            co.contract_id,
            co.service_code,
            s.service_name,
            co.quantity,
            co.unit_price_cad,
            co.discount_pct,
            co.net_mrc_cad,
            co.status,
            co.start_date,
            co.end_date,
            co.sla_uptime_pct,
            co.promo_code
        FROM {cat}.{sch}.contracts co
        JOIN {cat}.{sch}.services  s ON s.service_code = co.service_code
        WHERE co.customer_id = ?
        ORDER BY co.contract_id
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [customer_id])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_invoice_lines(contract_id: str, from_period: str, to_period: str) -> List[Dict]:
    """Return invoice lines for a contract within a billing period range.

    Args:
        contract_id: The contract ID (e.g. "K-20012").
        from_period: Start billing period, format YYYY-MM (e.g. "2026-01").
        to_period: End billing period, format YYYY-MM (e.g. "2026-10").

    Returns:
        List of invoice line dicts with invoice_id, billing_period, line_id,
        charge_type, description, quantity, unit_price_cad, amount_cad.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT
            il.invoice_id,
            inv.billing_period,
            il.line_id,
            il.charge_type,
            il.description,
            il.quantity,
            il.unit_price_cad,
            il.amount_cad
        FROM {cat}.{sch}.invoice_lines il
        JOIN {cat}.{sch}.invoices      inv ON inv.invoice_id = il.invoice_id
        WHERE il.contract_id = ?
          AND inv.billing_period BETWEEN ? AND ?
        ORDER BY inv.billing_period, il.line_id
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [contract_id, from_period, to_period])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_outages(contract_id: str, from_date: str, to_date: str) -> List[Dict]:
    """Return service outages for a contract between two dates.

    Args:
        contract_id: The contract ID (e.g. "K-20022").
        from_date: Start date inclusive, format YYYY-MM-DD (e.g. "2026-08-01").
        to_date: End date inclusive, format YYYY-MM-DD (e.g. "2026-08-31").

    Returns:
        List of outage dicts with outage_id, ticket_id, category, start_time,
        end_time, duration_minutes, affected_units.
    """
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    from_dt = datetime.date.fromisoformat(from_date)
    to_dt = datetime.date.fromisoformat(to_date) + datetime.timedelta(days=1)
    sql = f"""
        SELECT
            outage_id,
            ticket_id,
            category,
            start_time,
            end_time,
            duration_minutes,
            affected_units
        FROM {cat}.{sch}.service_outages
        WHERE contract_id = ?
          AND start_time >= ?
          AND start_time < ?
        ORDER BY start_time
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [contract_id, from_dt, to_dt])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_request(request_id: str) -> Dict:
    """Return all columns for a single adjustment request.

    Args:
        request_id: The request ID to look up (e.g. "ADJ-10375").

    Returns:
        Dict of all adjustment_requests columns, or empty dict if not found.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"SELECT * FROM {cat}.{sch}.adjustment_requests WHERE request_id = ?"
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [request_id])
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        conn.close()
    return dict(zip(cols, row)) if row else {}


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def list_queue() -> List[Dict]:
    """Return all Pending Review requests with customer name and current status.

    Returns:
        List of dicts with request_id, customer_id, customer_name, contract_id,
        request_type, billing_periods, requested_amount_cad, submitted_date,
        submitted_by, ai_outcome, ai_amount_cad, final_outcome, final_amount_cad,
        decided_by, and status_now (latest final_outcome from review_decisions,
        or "Pending Review" if no decision has been recorded yet).
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT
            ar.request_id,
            ar.customer_id,
            c.customer_name,
            ar.contract_id,
            ar.request_type,
            ar.billing_periods,
            ar.requested_amount_cad,
            ar.submitted_date,
            ar.submitted_by,
            rd.ai_outcome,
            rd.ai_amount_cad,
            rd.final_outcome,
            rd.final_amount_cad,
            rd.decided_by
        FROM {cat}.{sch}.adjustment_requests ar
        JOIN {cat}.{sch}.customers c ON c.customer_id = ar.customer_id
        LEFT JOIN (
            SELECT rd1.*
            FROM {cat}.{sch}.review_decisions rd1
            JOIN (
                SELECT request_id, MAX(decided_at) AS max_dt
                FROM {cat}.{sch}.review_decisions
                GROUP BY request_id
            ) latest ON latest.request_id = rd1.request_id
                    AND latest.max_dt = rd1.decided_at
        ) rd ON rd.request_id = ar.request_id
        WHERE ar.status = 'Pending Review'
        ORDER BY ar.submitted_date
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        d = dict(zip(cols, row))
        d["status_now"] = d["final_outcome"] if d.get("final_outcome") else "Pending Review"
        results.append(d)
    return results


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_past_decisions(request_type: str) -> List[Dict]:
    """Return the last 3 decided requests of the given type for precedent review.

    Args:
        request_type: One of "Billing Error", "SLA Outage Credit",
            "Promotion Not Applied", "Duplicate Charge",
            or "Early Termination Fee Dispute".

    Returns:
        List of up to 3 dicts with request_id, request_type,
        approved_amount_cad, decision_reason, policy_reference, decision_date.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT
            request_id,
            request_type,
            approved_amount_cad,
            decision_reason,
            policy_reference,
            decision_date
        FROM {cat}.{sch}.adjustment_requests
        WHERE request_type = ?
          AND status IN ('Approved', 'Partially Approved', 'Rejected')
        ORDER BY decision_date DESC
        LIMIT 3
    """
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [request_type])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


# ---------------------------------------------------------------------------
# Check tools (read-only policy eligibility tools)
# ---------------------------------------------------------------------------

@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_rate_error(
    contract_id: str,
    from_period: str = "",
    to_period: str = "",
    as_of_date: str = "",
) -> Dict:
    """Check for billing rate errors on a contract per POL-101.

    Only counts months where the invoiced unit price is strictly higher than the
    contract price (POL-101 §2).  Returns ready-to-copy fields for the draft request.

    Args:
        contract_id: The contract ID to check (e.g. "K-20012").
        from_period: Earliest billing period to examine, format YYYY-MM. Defaults
            to 12 months before as_of_date.
        to_period: Latest billing period to examine, format YYYY-MM. Defaults to
            the month containing as_of_date.
        as_of_date: Reference date for lookback window, format YYYY-MM-DD. Defaults
            to today.

    Returns:
        Dict with keys:
            eligible (bool), amount (str "NNNN.NN"),
            request_type (str), billing_periods (str),
            invoice_ids (list[str] sorted), calculation (str),
            approval_needed (str), note (str),
            evidence_ids (list), policy_sections (list),
            periods_checked (list), periods_wrong (list),
            latest_invoice_still_wrong (bool).
    """
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    as_of = datetime.date.fromisoformat(as_of_date) if as_of_date else datetime.date.today()
    lookback_period = (as_of - datetime.timedelta(days=365)).strftime("%Y-%m")
    effective_to = to_period if to_period else as_of.strftime("%Y-%m")

    # Fetch contract price
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT unit_price_cad, discount_pct, quantity FROM {cat}.{sch}.contracts WHERE contract_id = ?",
            [contract_id],
        )
        row = cur.fetchone()
        if not row:
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "Billing Error", "billing_periods": "",
                "invoice_ids": [], "calculation": "Contract not found",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-101 §2", "POL-101 §3"],
                "periods_checked": [], "periods_wrong": [],
                "latest_invoice_still_wrong": False,
            }
        contract_price = Decimal(str(row[0]))
        discount_pct = Decimal(str(row[1]))
        quantity = Decimal(str(row[2]))

        # Fetch invoice lines (Recurring) for range, within 12-month lookback
        effective_from = max(from_period, lookback_period) if from_period else lookback_period
        cur.execute(
            f"""
            SELECT inv.billing_period, il.unit_price_cad, il.invoice_id
            FROM {cat}.{sch}.invoice_lines il
            JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
            WHERE il.contract_id = ?
              AND il.charge_type = 'Recurring'
              AND inv.billing_period BETWEEN ? AND ?
            ORDER BY inv.billing_period
            """,
            [contract_id, effective_from, effective_to],
        )
        cols = [d[0] for d in cur.description]
        lines = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Most recent Recurring line across ALL invoices for latest_invoice_still_wrong
        cur.execute(
            f"""
            SELECT il.unit_price_cad, inv.billing_period
            FROM {cat}.{sch}.invoice_lines il
            JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
            WHERE il.contract_id = ?
              AND il.charge_type = 'Recurring'
            ORDER BY inv.billing_period DESC
            LIMIT 1
            """,
            [contract_id],
        )
        latest_row = cur.fetchone()
    finally:
        conn.close()

    latest_invoice_still_wrong = False
    latest_period = ""
    if latest_row:
        latest_price = Decimal(str(latest_row[0]))
        latest_period = str(latest_row[1])
        latest_invoice_still_wrong = latest_price > contract_price

    # Only count months where invoiced price is strictly higher (POL-101 §2)
    seen_periods: dict = {}
    for line in lines:
        period = line["billing_period"]
        invoiced = Decimal(str(line["unit_price_cad"]))
        inv_id = line["invoice_id"]
        if invoiced > contract_price and period not in seen_periods:
            seen_periods[period] = {"invoiced": invoiced, "invoice_id": inv_id}

    periods_checked = sorted({l["billing_period"] for l in lines})
    periods_wrong = sorted(seen_periods.keys())

    # Build calculation string; show each month's piece when price varies
    invoice_ids: List[str] = sorted({seen_periods[p]["invoice_id"] for p in periods_wrong})
    total = Decimal("0.00")
    if periods_wrong:
        # Group by invoiced price to keep the string compact
        price_groups: dict = {}
        for p in periods_wrong:
            inv_price = seen_periods[p]["invoiced"]
            key = str(inv_price)
            price_groups.setdefault(key, {"price": inv_price, "count": 0, "credit": Decimal("0.00")})
            month_credit = calc_rate_error(inv_price, contract_price, quantity, discount_pct)
            price_groups[key]["count"] += 1
            price_groups[key]["credit"] += month_credit
            total += month_credit
        total = _cents(total)

        factor = (1 - discount_pct / 100)
        if len(price_groups) == 1:
            inv_price = list(price_groups.values())[0]["price"]
            n = len(periods_wrong)
            calc_str = (
                f"({inv_price:.2f} - {contract_price:.2f})"
                f" x {quantity} x {factor} x {n} month{'s' if n > 1 else ''}"
                f" = {total:,.2f}"
            )
        else:
            pieces = []
            for key in sorted(price_groups):
                g = price_groups[key]
                piece_total = _cents(g["credit"])
                pieces.append(
                    f"({g['price']:.2f} - {contract_price:.2f})"
                    f" x {quantity} x {factor} x {g['count']} month{'s' if g['count'] > 1 else ''}"
                    f" = {piece_total:,.2f}"
                )
            calc_str = "; ".join(pieces) + f"; total = {total:,.2f}"
    else:
        calc_str = "No months with invoiced price above contract price"

    if periods_wrong:
        bp = periods_wrong[0] if len(periods_wrong) == 1 else f"{periods_wrong[0]} to {periods_wrong[-1]}"
    else:
        bp = ""

    note = ""
    if latest_invoice_still_wrong and latest_period:
        note = (
            f"The {latest_period} invoice is still incorrect. "
            "A rate correction is also needed with Billing Operations (POL-101 §5)."
        )

    _pol = "POL-101 §3" if len(periods_wrong) > 0 else "POL-101 §2"
    return {
        "eligible": len(periods_wrong) > 0,
        "amount": str(total),
        "request_type": "Billing Error",
        "billing_periods": bp,
        "invoice_ids": invoice_ids,
        "calculation": calc_str,
        "approval_needed": calc_approval_level(total),
        "note": note,
        "evidence_ids": invoice_ids,
        "policy_sections": ["POL-101 §2", "POL-101 §3"],
        "policy_quote": f"{_pol}: {POLICY_QUOTES[_pol]}",
        "periods_checked": periods_checked,
        "periods_wrong": periods_wrong,
        "latest_invoice_still_wrong": latest_invoice_still_wrong,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_sla_credit(contract_id: str, month: str, as_of_date: str = "") -> Dict:
    """Check SLA credit eligibility for one billing month per POL-102.

    Args:
        contract_id: The contract ID to check (e.g. "K-20022").
        month: Billing month to examine, format YYYY-MM (e.g. "2026-08").
        as_of_date: Reference date for the 60-day filing window, format YYYY-MM-DD.
            Defaults to today.

    Returns:
        Dict with keys:
            eligible (bool), amount (str "NNNN.NN"),
            request_type (str), billing_periods (str),
            invoice_ids (list[str] sorted), calculation (str),
            approval_needed (str), note (str),
            evidence_ids (list), policy_sections (list),
            sla_eligible_service (bool), availability_pct (str),
            shortfall (str), credit_tier_pct (str),
            affected_units (int), days_since_outage_end (int), within_60_days (bool).
    """
    import calendar
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    as_of = datetime.date.fromisoformat(as_of_date) if as_of_date else datetime.date.today()
    year, mon = int(month[:4]), int(month[5:7])

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT service_code, sla_uptime_pct, unit_price_cad, discount_pct, quantity
                FROM {cat}.{sch}.contracts WHERE contract_id = ?""",
            [contract_id],
        )
        crow = cur.fetchone()
        if not crow:
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "SLA Outage Credit", "billing_periods": month,
                "invoice_ids": [], "calculation": "Contract not found",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-102 §1"],
                "policy_quote": f"POL-102 §1: {POLICY_QUOTES['POL-102 §1']}",
                "sla_eligible_service": False, "availability_pct": None,
                "shortfall": None, "credit_tier_pct": None, "affected_units": None,
                "days_since_outage_end": None, "within_60_days": False,
                "service_code": None, "quantity": None,
            }

        service_code = crow[0]
        sla_uptime_str = crow[1]
        quantity = int(crow[4]) if crow[4] is not None else None
        sla_eligible_service = sla_uptime_str is not None and str(sla_uptime_str).strip() != ""
        if not sla_eligible_service:
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "SLA Outage Credit", "billing_periods": month,
                "invoice_ids": [], "calculation": "Service has no SLA",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-102 §1"],
                "policy_quote": f"POL-102 §1: {POLICY_QUOTES['POL-102 §1']}",
                "sla_eligible_service": False, "availability_pct": None,
                "shortfall": None, "credit_tier_pct": None, "affected_units": None,
                "days_since_outage_end": None, "within_60_days": False,
                "service_code": service_code, "quantity": quantity,
            }

        sla_target = Decimal(str(sla_uptime_str))
        unit_price = Decimal(str(crow[2]))
        discount_pct = Decimal(str(crow[3]))

        # Month boundaries as dates for the outage query
        month_start = datetime.date(year, mon, 1)
        last_day = calendar.monthrange(year, mon)[1]
        month_end = datetime.date(year, mon, last_day)
        next_day = month_end + datetime.timedelta(days=1)

        cur.execute(
            f"""SELECT outage_id, ticket_id, duration_minutes, affected_units, end_time
                FROM {cat}.{sch}.service_outages
                WHERE contract_id = ?
                  AND category = 'Network'
                  AND start_time >= ?
                  AND start_time < ?""",
            [contract_id, month_start, next_day],
        )
        outage_rows = cur.fetchall()

        # Customer's invoice ID for the billing month (for invoice_ids field)
        cur.execute(
            f"""SELECT DISTINCT inv.invoice_id
                FROM {cat}.{sch}.invoice_lines il
                JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
                WHERE il.contract_id = ?
                  AND inv.billing_period = ?
                LIMIT 1""",
            [contract_id, month],
        )
        inv_row = cur.fetchone()
        invoice_id_for_month = inv_row[0] if inv_row else None
    finally:
        conn.close()

    if not outage_rows:
        return {
            "eligible": False, "amount": "0.00",
            "request_type": "SLA Outage Credit", "billing_periods": month,
            "invoice_ids": [], "calculation": "No Network outages in month",
            "approval_needed": "", "note": "",
            "evidence_ids": [], "policy_sections": ["POL-102 §4"],
            "policy_quote": f"POL-102 §4: {POLICY_QUOTES['POL-102 §4']}",
            "sla_eligible_service": True, "availability_pct": str(Decimal("100")),
            "shortfall": "0", "credit_tier_pct": "0", "affected_units": 0,
            "days_since_outage_end": None, "within_60_days": False,
            "service_code": service_code, "quantity": quantity,
        }

    total_outage_minutes = sum(r[2] for r in outage_rows)
    max_affected_units = max(r[3] for r in outage_rows)
    ticket_ids = [r[1] for r in outage_rows]

    # Latest end_time for 60-day check
    end_times = []
    for r in outage_rows:
        et = r[4]
        if isinstance(et, str):
            et = datetime.datetime.fromisoformat(et)
        if hasattr(et, 'date'):
            et = et.date()
        end_times.append(et)
    latest_end = max(end_times)
    days_since = (as_of - latest_end).days
    within_60_days = days_since <= 60

    month_minutes = Decimal(str(last_day * 1440))
    availability = calc_sla_availability(Decimal(str(total_outage_minutes)), month_minutes)
    shortfall = sla_target - availability
    credit_pct = calc_sla_credit_pct(shortfall)
    credit = calc_sla_credit(unit_price, Decimal(str(max_affected_units)), discount_pct, credit_pct)

    eligible = bool(credit_pct > 0 and within_60_days)

    invoice_ids_sla: List[str] = [invoice_id_for_month] if invoice_id_for_month else []
    calc_sla_str = (
        f"{unit_price:.2f} x {max_affected_units} units"
        f" x {(1 - discount_pct/100)} x {credit_pct}%"
        f" = {credit:,.2f}"
    )

    _sla_pol = "POL-102 §5" if not within_60_days else "POL-102 §4"
    return {
        "eligible": eligible,
        "amount": str(credit),
        "request_type": "SLA Outage Credit",
        "billing_periods": month,
        "invoice_ids": invoice_ids_sla,
        "calculation": calc_sla_str,
        "approval_needed": calc_approval_level(credit) if eligible else "",
        "note": "" if within_60_days else "Outage is outside the 60-day filing window (POL-102 §5).",
        "evidence_ids": ticket_ids,
        "policy_sections": ["POL-102 §2", "POL-102 §4", "POL-102 §5"],
        "policy_quote": f"{_sla_pol}: {POLICY_QUOTES[_sla_pol]}",
        "sla_eligible_service": True,
        "availability_pct": str(availability),
        "shortfall": str(shortfall),
        "credit_tier_pct": str(credit_pct),
        "affected_units": max_affected_units,
        "days_since_outage_end": days_since,
        "within_60_days": within_60_days,
        "service_code": service_code, "quantity": quantity,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_promotion(contract_id: str, from_period: str = "", to_period: str = "") -> Dict:
    """Check whether a promotional credit was missing or short on invoices per POL-103.

    Args:
        contract_id: The contract ID to check (e.g. "K-20033").
        from_period: Earliest billing period to examine, format YYYY-MM. Defaults
            to the promotion start period on the contract.
        to_period: Latest billing period to examine, format YYYY-MM. Defaults
            to the promotion end period on the contract.

    Returns:
        Dict with keys:
            eligible (bool), amount (str "NNNN.NN"),
            request_type (str), billing_periods (str),
            invoice_ids (list[str] sorted), calculation (str),
            approval_needed (str), note (str),
            evidence_ids (list), policy_sections (list),
            promo_code (str), promo_period (str), periods_missing (list).
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT promo_code, promo_monthly_credit_cad, promo_start_period, promo_end_period
                FROM {cat}.{sch}.contracts WHERE contract_id = ?""",
            [contract_id],
        )
        crow = cur.fetchone()
        if not crow or not crow[0] or str(crow[0]).strip() == "":
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "Promotion Not Applied", "billing_periods": "",
                "invoice_ids": [], "calculation": "No promotion on contract",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-103 §2"],
                "policy_quote": f"POL-103 §2: {POLICY_QUOTES['POL-103 §2']}",
                "promo_code": None, "promo_period": None, "periods_missing": [],
            }

        promo_code = crow[0]
        monthly_credit = Decimal(str(crow[1]))
        promo_start = crow[2]  # YYYY-MM string
        promo_end = crow[3]

        # Default to the full promo window if caller did not specify a range
        effective_from = from_period if from_period else str(promo_start)
        effective_to = to_period if to_period else str(promo_end)

        # Fetch Promotional Credit lines in range
        cur.execute(
            f"""SELECT inv.billing_period, SUM(il.amount_cad) AS credited, inv.invoice_id
                FROM {cat}.{sch}.invoice_lines il
                JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
                WHERE il.contract_id = ?
                  AND il.charge_type = 'Promotional Credit'
                  AND inv.billing_period BETWEEN ? AND ?
                GROUP BY inv.billing_period, inv.invoice_id
                ORDER BY inv.billing_period""",
            [contract_id, effective_from, effective_to],
        )
        credited_rows = cur.fetchall()
    finally:
        conn.close()

    credited_by_period = {r[0]: (Decimal(str(r[1])), r[2]) for r in credited_rows}

    # Build list of periods in range that fall within the promo window
    def _periods_in_range(start, end):
        import datetime
        result = []
        y, m = int(start[:4]), int(start[5:7])
        ey, em = int(end[:4]), int(end[5:7])
        while (y, m) <= (ey, em):
            result.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m = 1; y += 1
        return result

    req_start = max(effective_from, str(promo_start))
    req_end = min(effective_to, str(promo_end))
    if req_start > req_end:
        return {
            "eligible": False, "amount": "0.00",
            "request_type": "Promotion Not Applied", "billing_periods": "",
            "invoice_ids": [], "calculation": "Requested range outside promo period",
            "approval_needed": "", "note": "",
            "evidence_ids": [], "policy_sections": ["POL-103 §2"],
            "policy_quote": f"POL-103 §2: {POLICY_QUOTES['POL-103 §2']}",
            "promo_code": promo_code, "promo_period": f"{promo_start} to {promo_end}",
            "periods_missing": [],
        }

    periods = _periods_in_range(req_start, req_end)
    total = Decimal("0.00")
    periods_missing = []
    evidence_ids = []
    for p in periods:
        if p not in credited_by_period:
            total += monthly_credit
            periods_missing.append(p)
        else:
            actual, inv_id = credited_by_period[p]
            evidence_ids.append(inv_id)
            diff = monthly_credit - abs(actual)
            if diff > 0:
                total += diff
                periods_missing.append(p)

    total_cents = _cents(total)
    invoice_ids_promo: List[str] = sorted(set(evidence_ids))
    n = len(periods_missing)
    bp_promo = (periods_missing[0] if n == 1
                else f"{periods_missing[0]} to {periods_missing[-1]}" if n > 1
                else "")
    calc_promo_str = (
        f"{monthly_credit:.2f} x {n} missing/short period{'s' if n != 1 else ''}"
        f" = {total_cents:,.2f}"
    ) if n > 0 else "No missing or short promotional credits"

    _promo_pol = "POL-103 §3" if total > 0 else "POL-103 §2"
    return {
        "eligible": total > 0,
        "amount": str(total_cents),
        "request_type": "Promotion Not Applied",
        "billing_periods": bp_promo,
        "invoice_ids": invoice_ids_promo,
        "calculation": calc_promo_str,
        "approval_needed": calc_approval_level(total_cents) if total > 0 else "",
        "note": "",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-103 §2", "POL-103 §3"],
        "policy_quote": f"{_promo_pol}: {POLICY_QUOTES[_promo_pol]}",
        "promo_code": promo_code,
        "promo_period": f"{promo_start} to {promo_end}",
        "periods_missing": periods_missing,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_duplicates(contract_id: str, period: str) -> Dict:
    """Check for duplicate Recurring invoice lines for a contract in a billing period per POL-104.

    Args:
        contract_id: The contract ID to check (e.g. "K-20044").
        period: Billing period to examine, format YYYY-MM (e.g. "2026-10").

    Returns:
        Dict with keys:
            eligible (bool), amount (str "NNNN.NN"),
            request_type (str), billing_periods (str),
            invoice_ids (list[str] sorted), calculation (str),
            approval_needed (str), note (str),
            evidence_ids (list), policy_sections (list),
            duplicate_sets (list).
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT il.charge_type, il.description, il.unit_price_cad, il.amount_cad,
                       il.line_id, il.invoice_id
                FROM {cat}.{sch}.invoice_lines il
                JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
                WHERE il.contract_id = ?
                  AND il.charge_type = 'Recurring'
                  AND inv.billing_period = ?
                ORDER BY il.description""",
            [contract_id, period],
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        key = (r[0], r[1], r[2], r[3])
        groups[key].append({"line_id": r[4], "invoice_id": r[5], "amount_cad": r[3]})

    total = Decimal("0.00")
    duplicate_sets = []
    evidence_ids: list = []
    for key, items in groups.items():
        if len(items) > 1:
            extra_copies = items[1:]
            extra_total = sum(Decimal(str(i["amount_cad"])) for i in extra_copies)
            total += extra_total
            duplicate_sets.append({
                "description": key[1],
                "count": len(items),
                "extra_amount_cad": str(_cents(extra_total)),
            })
            for i in items:
                evidence_ids.append(i["invoice_id"])
                evidence_ids.append(i["line_id"])

    evidence_ids = list(dict.fromkeys(evidence_ids))

    total_dup = _cents(total)
    # invoice IDs only (no line IDs) from duplicate groups
    inv_ids_only: List[str] = sorted({eid for eid in evidence_ids
                                       if not str(eid).startswith("L-")})
    n_dup = len(duplicate_sets)
    calc_dup_str = (
        f"{n_dup} duplicate set{'s' if n_dup != 1 else ''} found: {total_dup:,.2f}"
        if n_dup > 0 else "No duplicate Recurring lines found"
    )

    _dup_pol = "POL-104 §3" if total > 0 else "POL-104 §1"
    return {
        "eligible": total > 0,
        "amount": str(total_dup),
        "request_type": "Duplicate Charge",
        "billing_periods": period,
        "invoice_ids": inv_ids_only,
        "calculation": calc_dup_str,
        "approval_needed": calc_approval_level(total_dup) if total > 0 else "",
        "note": "",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-104 §1", "POL-104 §3"],
        "policy_quote": f"{_dup_pol}: {POLICY_QUOTES[_dup_pol]}",
        "duplicate_sets": duplicate_sets,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_etf(contract_id: str, request_text: str) -> Dict:
    """Check early termination fee dispute eligibility per POL-105.

    Args:
        contract_id: The contract ID to check (e.g. "K-20060").
        request_text: Free text from the account manager describing the dispute;
            used to detect a relocation service-change number (e.g. "SC-1234").

    Returns:
        Dict with keys:
            eligible (bool), amount (str "NNNN.NN"),
            request_type (str), billing_periods (str),
            invoice_ids (list[str] sorted), calculation (str),
            approval_needed (str), note (str),
            evidence_ids (list), policy_sections (list),
            billed_etf (str), expected_etf (str),
            waiver_a (dict), waiver_b (dict), waiver_c (dict).
    """
    import datetime
    import re
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT net_mrc_cad, termination_date, end_date, status, term_months,
                       start_date, replaces_contract_id, sla_uptime_pct
                FROM {cat}.{sch}.contracts WHERE contract_id = ?""",
            [contract_id],
        )
        crow = cur.fetchone()
        if not crow:
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "Early Termination Fee Dispute", "billing_periods": "",
                "invoice_ids": [], "calculation": "Contract not found",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-105 §1", "POL-105 §2"],
                "billed_etf": "0.00", "expected_etf": "0.00",
                "waiver_a": {"qualifies": False, "months_below_sla": 0},
                "waiver_b": {"qualifies": False, "sc_number": None},
                "waiver_c": {"qualifies": False, "new_contract_id": None},
            }

        net_mrc = Decimal(str(crow[0]))
        term_date_raw = crow[1]
        end_date_raw = crow[2]
        replaces_id = crow[6]
        sla_uptime_str = crow[7]

        def _to_date(v):
            if v is None:
                return None
            if isinstance(v, datetime.date):
                return v
            return datetime.date.fromisoformat(str(v)[:10])

        term_date = _to_date(term_date_raw)
        end_date = _to_date(end_date_raw)

        if term_date is None or end_date is None:
            return {
                "eligible": False, "amount": "0.00",
                "request_type": "Early Termination Fee Dispute", "billing_periods": "",
                "invoice_ids": [], "calculation": "Missing termination or end date",
                "approval_needed": "", "note": "",
                "evidence_ids": [], "policy_sections": ["POL-105 §1"],
                "billed_etf": "0.00", "expected_etf": "0.00",
                "waiver_a": {"qualifies": False, "months_below_sla": 0},
                "waiver_b": {"qualifies": False, "sc_number": None},
                "waiver_c": {"qualifies": False, "new_contract_id": None},
            }

        # remaining_months = full months from month after termination_date to end_date inclusive
        start_count = datetime.date(term_date.year + (term_date.month // 12),
                                    (term_date.month % 12) + 1, 1)
        remaining_months = (end_date.year - start_count.year) * 12 + (end_date.month - start_count.month) + 1
        if remaining_months < 0:
            remaining_months = 0
        expected_etf = calc_etf(net_mrc, Decimal(str(remaining_months)))

        # Billed ETF from invoice lines
        term_period = term_date.strftime("%Y-%m")
        cur.execute(
            f"""SELECT il.amount_cad, il.invoice_id
                FROM {cat}.{sch}.invoice_lines il
                JOIN {cat}.{sch}.invoices inv ON inv.invoice_id = il.invoice_id
                WHERE il.contract_id = ?
                  AND il.charge_type = 'Early Termination Fee'
                  AND inv.billing_period = ?
                LIMIT 1""",
            [contract_id, term_period],
        )
        etf_row = cur.fetchone()
        billed_etf = Decimal(str(etf_row[0])) if etf_row else Decimal("0.00")
        etf_invoice_id = etf_row[1] if etf_row else None

        # Waiver A: chronic SLA failure (only if SLA service)
        waiver_a = False
        months_below_sla = 0
        months_below_sla_list: List[str] = []
        if sla_uptime_str is not None and str(sla_uptime_str).strip() != "":
            import calendar
            sla_target = Decimal(str(sla_uptime_str))
            # 6 billing periods up to and including termination month
            waiver_months = []
            y, m = term_date.year, term_date.month
            for _ in range(6):
                waiver_months.append((y, m))
                m -= 1
                if m < 1:
                    m = 12; y -= 1
            for (wy, wm) in waiver_months:
                wstart = datetime.date(wy, wm, 1)
                wlast = calendar.monthrange(wy, wm)[1]
                wend = datetime.date(wy, wm, wlast)
                wnext = wend + datetime.timedelta(days=1)
                cur.execute(
                    f"""SELECT SUM(duration_minutes)
                        FROM {cat}.{sch}.service_outages
                        WHERE contract_id = ?
                          AND category = 'Network'
                          AND start_time >= ?
                          AND start_time < ?""",
                    [contract_id, wstart, wnext],
                )
                orow = cur.fetchone()
                om = Decimal(str(orow[0])) if orow and orow[0] else Decimal("0")
                month_mins = Decimal(str(wlast * 1440))
                avail = calc_sla_availability(om, month_mins)
                if avail < sla_target:
                    months_below_sla += 1
                    months_below_sla_list.append(f"{wy:04d}-{wm:02d}")
            waiver_a = months_below_sla >= 3

        # Waiver B: relocation SC- number
        sc_match = re.search(r"SC-\d+", request_text or "")
        waiver_b = sc_match is not None
        sc_number = sc_match.group(0) if sc_match else None

        # Waiver C: upgrade (new contract replaced this one, signed within 30 days, term >= original, mrc >= original)
        waiver_c = False
        new_contract_id = None
        original_term = crow[4]
        original_mrc = net_mrc

        cur.execute(
            f"""SELECT contract_id, start_date, term_months, net_mrc_cad
                FROM {cat}.{sch}.contracts
                WHERE replaces_contract_id = ?""",
            [contract_id],
        )
        upgrade_row = cur.fetchone()
        if upgrade_row:
            new_cid = upgrade_row[0]
            new_start = _to_date(upgrade_row[1])
            new_term = upgrade_row[2]
            new_mrc = Decimal(str(upgrade_row[3]))
            if (new_start and term_date and
                    abs((new_start - term_date).days) <= 30 and
                    new_term >= original_term and
                    new_mrc >= original_mrc):
                waiver_c = True
                new_contract_id = new_cid
    finally:
        conn.close()

    evidence_ids = []
    if etf_invoice_id:
        evidence_ids.append(etf_invoice_id)
    if new_contract_id:
        evidence_ids.append(new_contract_id)

    if waiver_a or waiver_b or waiver_c:
        eligible = billed_etf > 0
        amount = str(billed_etf)
        calc_str = "Full waiver — billed ETF refunded"
    elif billed_etf != expected_etf and billed_etf > expected_etf:
        eligible = True
        amount = str(_cents(billed_etf - expected_etf))
        calc_str = f"Billed {billed_etf} > expected {expected_etf}"
    else:
        eligible = False
        amount = "0.00"
        calc_str = f"Billed {billed_etf} matches expected {expected_etf}"

    amount_dec = Decimal(amount)
    term_period_str = term_date.strftime("%Y-%m") if term_date else ""
    etf_invoice_ids: List[str] = sorted({eid for eid in evidence_ids
                                          if str(eid).startswith("INV-")})

    if waiver_a or waiver_b or waiver_c:
        _etf_pol = "POL-105 §2"
    elif eligible:
        _etf_pol = "POL-105 §1"
    else:
        _etf_pol = "POL-105 §4"
    return {
        "eligible": eligible,
        "amount": amount,
        "request_type": "Early Termination Fee Dispute",
        "billing_periods": term_period_str,
        "invoice_ids": etf_invoice_ids,
        "calculation": calc_str,
        "approval_needed": calc_approval_level(amount_dec) if eligible else "Reviewer",
        "note": "",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-105 §1", "POL-105 §2"],
        "policy_quote": f"{_etf_pol}: {POLICY_QUOTES[_etf_pol]}",
        "billed_etf": str(billed_etf),
        "expected_etf": str(expected_etf),
        "waiver_a": {"qualifies": waiver_a, "months_below_sla": months_below_sla,
                     "months_below_sla_list": months_below_sla_list},
        "waiver_b": {"qualifies": waiver_b, "sc_number": sc_number},
        "waiver_c": {"qualifies": waiver_c, "new_contract_id": new_contract_id},
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_completeness(request_id: str) -> Dict:
    """Check whether an adjustment request has all POL-000 §3 required fields.

    Args:
        request_id: The request ID to check (e.g. "ADJ-10375").

    Returns:
        Dict with keys complete (bool), missing_items (list of str describing
        each gap), and policy_sections (list).
    """
    import re
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM {cat}.{sch}.adjustment_requests WHERE request_id = ?",
            [request_id],
        )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"complete": False, "missing_items": ["Request not found"],
                "policy_sections": ["POL-000 §3"]}

    req = dict(zip(cols, row))

    def _present(val):
        return val is not None and str(val).strip() != ""

    missing = []
    if not _present(req.get("customer_id")):
        missing.append("1: customer_id missing")
    if not _present(req.get("contract_id")):
        missing.append("2: contract_id missing")
    if not _present(req.get("request_type")):
        missing.append("3: request_type missing")
    if not _present(req.get("billing_periods")):
        missing.append("4: billing_periods missing")
    if not _present(req.get("invoice_id")):
        missing.append("5: invoice_id missing")
    amt = req.get("requested_amount_cad")
    if not _present(amt) or Decimal(str(amt)) <= 0:
        missing.append("6: requested_amount_cad missing or zero")
    if req.get("request_type") == "SLA Outage Credit":
        evidence = str(req.get("evidence_provided") or "")
        description = str(req.get("description") or "")
        if not re.search(r"TKT-\d+", evidence + " " + description):
            missing.append("7: TKT- ticket ID required for SLA Outage Credit")

    return {
        "complete": len(missing) == 0,
        "missing_items": missing,
        "policy_sections": ["POL-000 §3"],
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def approval_level(amount: float) -> Dict:
    """Return the required approval level for a given credit amount per POL-001 §1.

    Args:
        amount: Credit amount in CAD (positive float).

    Returns:
        Dict with keys level (str: "Reviewer", "Director", or "Director and VP")
        and policy_section (str).
    """
    level = calc_approval_level(Decimal(str(amount)))
    return {"level": level, "policy_section": "POL-001 §1"}


# ---------------------------------------------------------------------------
# Write tools (state-changing)
# ---------------------------------------------------------------------------

@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def submit_request(
    customer_id: str,
    contract_id: str,
    invoice_id: str,
    request_type: str,
    billing_periods: str,
    requested_amount_cad: float,
    description: str,
    evidence_provided: str,
    submitted_by: str,
) -> Dict:
    """Insert a new adjustment request and return its generated request_id.

    Args:
        customer_id: Customer ID (e.g. "CUS-00042").
        contract_id: Contract ID (e.g. "K-20012").
        invoice_id: Comma-separated invoice ID(s) (e.g. "INV-1001,INV-1002").
        request_type: One of the five request type strings.
        billing_periods: One period or range (e.g. "2026-01 to 2026-10").
        requested_amount_cad: Amount requested in CAD.
        description: Free-text description of the issue from the account manager.
        evidence_provided: Comma-separated evidence IDs (invoice IDs, ticket IDs).
        submitted_by: Employee ID of the submitting account manager; auto-filled
            from the customer record if empty.

    Returns:
        Dict with keys request_id (str) and submitted_date (str YYYY-MM-DD).
    """
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()

        # Look up account_manager_id if submitted_by is empty
        if not submitted_by or not submitted_by.strip():
            cur.execute(
                f"SELECT account_manager_id FROM {cat}.{sch}.customers WHERE customer_id = ?",
                [customer_id],
            )
            am_row = cur.fetchone()
            submitted_by = am_row[0] if am_row else ""

        # Generate next request_id
        cur.execute(f"SELECT MAX(request_id) FROM {cat}.{sch}.adjustment_requests")
        max_row = cur.fetchone()
        max_id = max_row[0] if max_row and max_row[0] else "ADJ-09999"
        next_n = int(max_id.split("-")[1]) + 1
        request_id = f"ADJ-{next_n:05d}"

        submitted_date = datetime.date.today()

        cur.execute(
            f"""INSERT INTO {cat}.{sch}.adjustment_requests
                (request_id, customer_id, contract_id, invoice_id, request_type,
                 billing_periods, requested_amount_cad, description, evidence_provided,
                 submitted_by, submitted_date, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending Review')""",
            [
                request_id, customer_id, contract_id, invoice_id, request_type,
                billing_periods, float(requested_amount_cad), description,
                evidence_provided, submitted_by, submitted_date,
            ],
        )
    finally:
        conn.close()

    return {"request_id": request_id, "submitted_date": submitted_date.isoformat()}


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def record_decision(
    request_id: str,
    decided_by: str,
    ai_outcome: str,
    ai_amount_cad: float,
    final_outcome: str,
    final_amount_cad: float,
    override_reason: str,
    notes: str,
) -> Dict:
    """Record an AI or human decision against an adjustment request.

    Args:
        request_id: The request to decide (e.g. "ADJ-10375").
        decided_by: Employee ID or full name of the reviewer or director.
        ai_outcome: AI-recommended outcome from review_request (e.g. "Approved").
        ai_amount_cad: AI-recommended amount from review_request.
        final_outcome: The person's chosen outcome.
        final_amount_cad: The person's chosen amount.
        override_reason: Required when final differs from AI recommendation.
        notes: Optional free-text notes.

    Returns:
        Dict with keys request_id, final_outcome (may be escalated to
        "Recommended - awaiting Director"), and decided_at (ISO timestamp).
    """
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    conn = _connect(cfg)
    try:
        cur = conn.cursor()

        # Look up employee by employee_id or full_name (case-insensitive)
        cur.execute(
            f"""SELECT employee_id, full_name, role FROM {cat}.{sch}.employees
                WHERE employee_id = ? OR LOWER(full_name) = LOWER(?)
                LIMIT 1""",
            [decided_by, decided_by],
        )
        emp_row = cur.fetchone()
        if not emp_row:
            raise ValueError(f"Employee not found: {decided_by}")
        role = emp_row[2]
        if role not in ("Reviewer", "Director"):
            raise ValueError(f"Employee role '{role}' cannot record decisions")

        # Rule 2: override check (before Rule 1 modifies outcome)
        is_override = (final_outcome != ai_outcome) or (float(final_amount_cad) != float(ai_amount_cad))
        if is_override and not (override_reason and override_reason.strip()):
            raise ValueError("override_reason is required when final differs from ai recommendation")

        # Rule 3: Director can only act on Recommended - awaiting Director
        if role == "Director":
            cur.execute(
                f"""SELECT final_outcome FROM {cat}.{sch}.review_decisions
                    WHERE request_id = ?
                    ORDER BY decided_at DESC
                    LIMIT 1""",
                [request_id],
            )
            prev = cur.fetchone()
            if not prev or prev[0] != "Recommended - awaiting Director":
                raise ValueError("Director can only act when latest decision is 'Recommended - awaiting Director'")

        # Rule 1: escalate Reviewer decisions above $20,000
        if Decimal(str(final_amount_cad)) > Decimal("20000.00") and role == "Reviewer":
            saved_outcome = "Recommended - awaiting Director"
        else:
            saved_outcome = final_outcome

        decided_at = datetime.datetime.utcnow()
        approval_lvl = calc_approval_level(Decimal(str(final_amount_cad)))

        cur.execute(
            f"""INSERT INTO {cat}.{sch}.review_decisions
                (request_id, decided_at, decided_by, role, ai_outcome, ai_amount_cad,
                 final_outcome, final_amount_cad, approval_level_required, override_reason, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                request_id, decided_at, decided_by, role,
                ai_outcome, float(ai_amount_cad),
                saved_outcome, float(final_amount_cad),
                approval_lvl, override_reason, notes,
            ],
        )
    finally:
        conn.close()

    return {
        "request_id": request_id,
        "final_outcome": saved_outcome,
        "decided_at": decided_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Review tool (read-only, deterministic recommendation)
# ---------------------------------------------------------------------------

@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def review_request(request_id: str) -> Dict:
    """Review an adjustment request and return a deterministic recommendation.

    Runs completeness checks (POL-000 §3, §8) then the appropriate eligibility
    check tool.  The recommendation comes entirely from code — the model must
    not change any number.

    Args:
        request_id: The request ID to review (e.g. "ADJ-10375").

    Returns:
        Dict with keys:
            request_id, customer_name, request_type, requested_amount (str),
            recommended_outcome (str), recommended_amount (str),
            approval_needed (str), reason (str), calculation (str),
            invoice_ids (list), evidence (list), policy_sections (list),
            policy_quote (str), note (str).
    """
    import re

    def _unwrap(v):
        """Extract plain dict/list from a ToolResponse if wrapped."""
        return v.content if hasattr(v, "content") else v

    # --- 1. Fetch the request ------------------------------------------------
    req = _unwrap(get_request(request_id))
    if not req:
        return {
            "request_id": request_id,
            "customer_name": "",
            "request_type": "",
            "requested_amount": "0.00",
            "recommended_outcome": "Returned for Information",
            "recommended_amount": "0.00",
            "approval_needed": "Reviewer",
            "reason": "Request not found.",
            "calculation": "",
            "invoice_ids": [],
            "evidence": [],
            "policy_sections": ["POL-000 §3"],
            "policy_quote": f"POL-000 §3: {POLICY_QUOTES['POL-000 §3']}",
            "note": "",
        }

    # --- 2. Look up customer name -------------------------------------------
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT customer_name FROM {cat}.{sch}.customers WHERE customer_id = ?",
            [req.get("customer_id", "")],
        )
        crow = cur.fetchone()
    finally:
        conn.close()
    customer_name = crow[0] if crow else ""

    requested_amount = Decimal(str(req.get("requested_amount_cad") or "0"))
    request_type = str(req.get("request_type") or "")
    contract_id = str(req.get("contract_id") or "")
    billing_periods = str(req.get("billing_periods") or "")
    submitted_date = str(req.get("submitted_date") or "")
    description = str(req.get("description") or "")
    evidence_provided = str(req.get("evidence_provided") or "")

    # --- 3. Completeness check (inline, same logic as check_completeness) ---
    def _present(val):
        return val is not None and str(val).strip() != ""

    missing = []
    if not _present(req.get("customer_id")):
        missing.append("customer_id missing")
    if not _present(req.get("contract_id")):
        missing.append("contract_id missing")
    if not _present(req.get("request_type")):
        missing.append("request_type missing")
    if not _present(req.get("billing_periods")):
        missing.append("billing_periods missing")
    if not _present(req.get("invoice_id")):
        missing.append("invoice_id missing")
    amt = req.get("requested_amount_cad")
    if not _present(amt) or Decimal(str(amt)) <= 0:
        missing.append("requested_amount_cad missing or zero")
    if request_type == "SLA Outage Credit":
        if not re.search(r"TKT-\d+", (evidence_provided + " " + description)):
            missing.append("TKT- ticket ID required for SLA Outage Credit (POL-000 §8)")

    if missing:
        return {
            "request_id": request_id,
            "customer_name": customer_name,
            "request_type": request_type,
            "requested_amount": f"{requested_amount:.2f}",
            "recommended_outcome": "Returned for Information",
            "recommended_amount": "0.00",
            "approval_needed": "Reviewer",
            "reason": "Missing required fields: " + "; ".join(missing) + ".",
            "calculation": "",
            "invoice_ids": [],
            "evidence": [],
            "policy_sections": ["POL-000 §3"],
            "policy_quote": f"POL-000 §3: {POLICY_QUOTES['POL-000 §3']}",
            "note": "",
        }

    # --- 4. Parse billing_periods into from/to for tools that need a range --
    # Formats: "YYYY-MM" or "YYYY-MM to YYYY-MM"
    bp_parts = billing_periods.split(" to ")
    bp_from = bp_parts[0].strip()
    bp_to = bp_parts[-1].strip()  # same as from if single period

    as_of = submitted_date[:10] if submitted_date else ""

    # --- 5. Run the matching check tool ------------------------------------
    check: Dict = {}
    if request_type == "Billing Error":
        check = _unwrap(check_rate_error(contract_id, bp_from, bp_to, as_of))
    elif request_type == "SLA Outage Credit":
        # check_sla_credit takes a single month
        check = _unwrap(check_sla_credit(contract_id, bp_from, as_of))
    elif request_type == "Promotion Not Applied":
        check = _unwrap(check_promotion(contract_id, bp_from, bp_to))
    elif request_type == "Duplicate Charge":
        check = _unwrap(check_duplicates(contract_id, bp_from))
    elif request_type == "Early Termination Fee Dispute":
        check = _unwrap(check_etf(contract_id, description + " " + evidence_provided))
    else:
        return {
            "request_id": request_id,
            "customer_name": customer_name,
            "request_type": request_type,
            "requested_amount": f"{requested_amount:.2f}",
            "recommended_outcome": "Returned for Information",
            "recommended_amount": "0.00",
            "approval_needed": "Reviewer",
            "reason": f"Unknown request_type: {request_type}",
            "calculation": "",
            "invoice_ids": [],
            "evidence": [],
            "policy_sections": ["POL-000 §3"],
            "policy_quote": f"POL-000 §3: {POLICY_QUOTES['POL-000 §3']}",
            "note": "",
        }

    # Normalise check values
    supported = Decimal(str(check.get("amount") or "0"))
    eligible = bool(check.get("eligible"))

    # --- 6. Build plain-language reason from check facts --------------------
    def _build_reason(rt: str, ch: Dict, eligible: bool) -> str:
        """Build a plain-English reason sentence from the check result facts."""
        if rt == "Billing Error":
            pw = ch.get("periods_wrong") or []
            pc = ch.get("periods_checked") or []
            if not eligible:
                if not pw:
                    return (
                        f"Invoiced unit price matches the contract price in all "
                        f"{len(pc)} period(s) checked. No billing error found."
                    )
                return ch.get("calculation") or "Not eligible per policy."
            # Partially or fully approved — say which months were wrong
            months = ", ".join(pw)
            return (
                f"Invoice price exceeded the contract price in {len(pw)} of the "
                f"{len(pc)} period(s) checked ({months})."
            )

        if rt == "SLA Outage Credit":
            sla_ok = ch.get("sla_eligible_service")
            if not sla_ok:
                svc = ch.get("service_code") or contract_id
                return f"{svc} has no SLA, so it cannot get an SLA credit."
            days = ch.get("days_since_outage_end")
            w60 = ch.get("within_60_days")
            units = ch.get("affected_units") or 0
            quantity = ch.get("quantity")
            ticket_ids = ch.get("evidence_ids") or []
            ticket = ticket_ids[0] if ticket_ids else "the outage"
            if not w60 and days is not None:
                return (
                    f"Submitted {days} days after the outage ended. "
                    f"The limit is 60 days."
                )
            if not eligible:
                return ch.get("calculation") or "Not eligible per policy."
            if quantity:
                return (
                    f"Outage {ticket} affected {units} of the {quantity} units on the contract."
                )
            return (
                f"Outage {ticket} affected {units} unit(s)."
            )

        if rt == "Promotion Not Applied":
            pm = ch.get("periods_missing") or []
            if not eligible:
                if not pm:
                    return "No missing or short promotional credits found in the requested period."
                return ch.get("calculation") or "Not eligible per policy."
            months = ", ".join(pm)
            return f"Promotional credit missing in {months}."

        if rt == "Duplicate Charge":
            ds = ch.get("duplicate_sets") or []
            if not eligible:
                return "No duplicate Recurring charges found for this contract and period."
            parts = []
            inv_ids = ch.get("invoice_ids") or []
            inv_str = ", ".join(inv_ids) if inv_ids else "the invoice"
            for d in ds:
                parts.append(f'{d["description"]} (extra copy on {inv_str})')
            return "Duplicate charge found: " + "; ".join(parts) + "."

        if rt == "Early Termination Fee Dispute":
            wa = ch.get("waiver_a") or {}
            wb = ch.get("waiver_b") or {}
            wc = ch.get("waiver_c") or {}
            n_months = wa.get("months_below_sla", 0)
            months_list = wa.get("months_below_sla_list") or []
            if wa.get("qualifies"):
                months_str = ", ".join(months_list) if months_list else f"{n_months} months"
                return (
                    f"Chronic failure waiver applies: "
                    f"in the 6 billing periods up to and including the termination month, "
                    f"the contract had at least 3 months where measured availability was "
                    f"below the SLA target (POL-102 §2 to §4) — specifically {months_str}."
                )
            if wb.get("qualifies"):
                sc = wb.get("sc_number", "")
                return f"Relocation waiver applies: serviceability check {sc} on file."
            if wc.get("qualifies"):
                new_c = wc.get("new_contract_id", "")
                return f"Upgrade waiver applies: replacement contract {new_c} signed within 30 days."
            billed = ch.get("billed_etf", "")
            expected = ch.get("expected_etf", "")
            if eligible:
                return (
                    f"ETF was miscalculated: billed {billed}, policy formula gives {expected}. "
                    f"Credit is the difference."
                )
            return (
                f"The fee matches the policy formula ({billed}) and no waiver applies."
            )

        return ch.get("calculation") or "Not eligible per policy."

    reason = _build_reason(request_type, check, eligible)

    # --- 7. Derive recommendation ------------------------------------------
    if not eligible:
        recommended_outcome = "Rejected"
        recommended_amount = Decimal("0.00")
    elif supported >= requested_amount:
        recommended_outcome = "Approved"
        recommended_amount = requested_amount
    else:
        recommended_outcome = "Partially Approved"
        recommended_amount = supported

    # approval_needed is always set — Reviewer for zero-amount outcomes too
    approval = calc_approval_level(recommended_amount) if recommended_amount > 0 else "Reviewer"

    policy_quote = check.get("policy_quote") or ""

    return {
        "request_id": request_id,
        "customer_name": customer_name,
        "request_type": request_type,
        "requested_amount": f"{requested_amount:.2f}",
        "recommended_outcome": recommended_outcome,
        "recommended_amount": f"{recommended_amount:.2f}",
        "approval_needed": approval,
        "reason": reason,
        "calculation": check.get("calculation") or "",
        "invoice_ids": check.get("invoice_ids") or [],
        "evidence": check.get("evidence_ids") or [],
        "policy_sections": check.get("policy_sections") or [],
        "policy_quote": policy_quote,
        "note": check.get("note") or "",
    }
