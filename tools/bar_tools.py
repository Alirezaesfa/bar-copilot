"""Maplewave billing-adjustment tools — Presto/Iceberg backed."""

import os
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import tool

APP_ID = "maplewave_data"

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
    """Return SLA credit percentage (5, 10, 25, or 0) per POL-102 §4 tier table."""
    if shortfall >= Decimal("1"):
        return Decimal("25")
    elif shortfall >= Decimal("0.5"):
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

@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def find_customers(name_text: str) -> List[Dict]:
    """Search customers by name; returns at most 5 matches to keep responses concise."""
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
    """Return all contracts for a confirmed customer_id, joined with service names."""
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
    """Return invoice lines for a contract within a billing period range."""
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
    """Return service outages for a contract between from_date and to_date (inclusive)."""
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
    """Return all columns for a single adjustment request, or empty dict if not found."""
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
    """Return all Pending Review requests with the latest AI/human decision columns."""
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]
    sql = f"""
        SELECT
            ar.request_id,
            ar.customer_id,
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
    return [dict(zip(cols, row)) for row in rows]


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def get_past_decisions(request_type: str) -> List[Dict]:
    """Return the last 3 decided requests of the given type for precedent review."""
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
    from_period: str,
    to_period: str,
    as_of_date: str,
) -> Dict:
    """Check for billing rate errors on a contract per POL-101."""
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    as_of = datetime.date.fromisoformat(as_of_date)
    lookback_period = (as_of - datetime.timedelta(days=365)).strftime("%Y-%m")

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
            return {"eligible": False, "amount": "0.00", "calculation": "Contract not found",
                    "evidence_ids": [], "policy_sections": ["POL-101 §2", "POL-101 §3"],
                    "periods_checked": [], "periods_wrong": [], "latest_invoice_still_wrong": False}
        contract_price = Decimal(str(row[0]))
        discount_pct = Decimal(str(row[1]))
        quantity = Decimal(str(row[2]))

        # Fetch invoice lines (Recurring) for range, within 12-month lookback
        effective_from = max(from_period, lookback_period)
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
            [contract_id, effective_from, to_period],
        )
        cols = [d[0] for d in cur.description]
        lines = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Most recent Recurring line across ALL invoices for latest_invoice_still_wrong
        cur.execute(
            f"""
            SELECT il.unit_price_cad
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
    if latest_row:
        latest_price = Decimal(str(latest_row[0]))
        latest_invoice_still_wrong = latest_price != contract_price

    # Accumulate periods and amounts
    seen_periods: dict = {}
    for line in lines:
        period = line["billing_period"]
        invoiced = Decimal(str(line["unit_price_cad"]))
        inv_id = line["invoice_id"]
        if invoiced != contract_price:
            if period not in seen_periods:
                seen_periods[period] = {"invoiced": invoiced, "invoice_id": inv_id}

    periods_checked = sorted({l["billing_period"] for l in lines})
    periods_wrong = sorted(seen_periods.keys())
    total = sum(
        calc_rate_error(seen_periods[p]["invoiced"], contract_price, quantity, discount_pct)
        for p in periods_wrong
    )
    evidence_ids = list({seen_periods[p]["invoice_id"] for p in periods_wrong})

    return {
        "eligible": len(periods_wrong) > 0,
        "amount": str(total),
        "calculation": f"({seen_periods[periods_wrong[0]]['invoiced'] if periods_wrong else 'N/A'} - {contract_price}) x {quantity} x {1 - discount_pct/100} x {len(periods_wrong)} period(s)",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-101 §2", "POL-101 §3"],
        "periods_checked": periods_checked,
        "periods_wrong": periods_wrong,
        "latest_invoice_still_wrong": latest_invoice_still_wrong,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_sla_credit(contract_id: str, month: str, as_of_date: str) -> Dict:
    """Check SLA credit eligibility for one billing month per POL-102."""
    import calendar
    import datetime
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    as_of = datetime.date.fromisoformat(as_of_date)
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
            return {"eligible": False, "amount": "0.00", "calculation": "Contract not found",
                    "evidence_ids": [], "policy_sections": ["POL-102 §2", "POL-102 §4"],
                    "sla_eligible_service": False, "availability_pct": None,
                    "shortfall": None, "credit_tier_pct": None, "affected_units": None,
                    "days_since_outage_end": None, "within_60_days": False}

        sla_uptime_str = crow[1]
        sla_eligible_service = sla_uptime_str is not None and str(sla_uptime_str).strip() != ""
        if not sla_eligible_service:
            return {"eligible": False, "amount": "0.00",
                    "calculation": "Service has no SLA",
                    "evidence_ids": [], "policy_sections": ["POL-102 §2", "POL-102 §4"],
                    "sla_eligible_service": False, "availability_pct": None,
                    "shortfall": None, "credit_tier_pct": None, "affected_units": None,
                    "days_since_outage_end": None, "within_60_days": False}

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
    finally:
        conn.close()

    if not outage_rows:
        return {"eligible": False, "amount": "0.00",
                "calculation": "No Network outages in month",
                "evidence_ids": [], "policy_sections": ["POL-102 §2", "POL-102 §4"],
                "sla_eligible_service": True, "availability_pct": str(Decimal("100")),
                "shortfall": "0", "credit_tier_pct": "0", "affected_units": 0,
                "days_since_outage_end": None, "within_60_days": False}

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

    return {
        "eligible": eligible,
        "amount": str(credit),
        "calculation": f"{unit_price} x {max_affected_units} units x {1 - discount_pct/100} x {credit_pct}%",
        "evidence_ids": ticket_ids,
        "policy_sections": ["POL-102 §2", "POL-102 §4", "POL-102 §5"],
        "sla_eligible_service": True,
        "availability_pct": str(availability),
        "shortfall": str(shortfall),
        "credit_tier_pct": str(credit_pct),
        "affected_units": max_affected_units,
        "days_since_outage_end": days_since,
        "within_60_days": within_60_days,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_promotion(contract_id: str, from_period: str, to_period: str) -> Dict:
    """Check whether a promotional credit was missing or short on invoices per POL-103."""
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
            return {"eligible": False, "amount": "0.00",
                    "calculation": "No promotion on contract",
                    "evidence_ids": [], "policy_sections": ["POL-103 §2", "POL-103 §3"],
                    "promo_code": None, "promo_period": None, "periods_missing": []}

        promo_code = crow[0]
        monthly_credit = Decimal(str(crow[1]))
        promo_start = crow[2]  # YYYY-MM string
        promo_end = crow[3]

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
            [contract_id, from_period, to_period],
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

    req_start = max(from_period, str(promo_start))
    req_end = min(to_period, str(promo_end))
    if req_start > req_end:
        return {"eligible": False, "amount": "0.00",
                "calculation": "Requested range outside promo period",
                "evidence_ids": [], "policy_sections": ["POL-103 §2", "POL-103 §3"],
                "promo_code": promo_code, "promo_period": f"{promo_start} to {promo_end}",
                "periods_missing": []}

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

    return {
        "eligible": total > 0,
        "amount": str(_cents(total)),
        "calculation": f"{monthly_credit} x {len(periods_missing)} missing/short period(s)",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-103 §2", "POL-103 §3"],
        "promo_code": promo_code,
        "promo_period": f"{promo_start} to {promo_end}",
        "periods_missing": periods_missing,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_duplicates(contract_id: str, period: str) -> Dict:
    """Check for duplicate Recurring invoice lines for a contract in a billing period per POL-104."""
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

    return {
        "eligible": total > 0,
        "amount": str(_cents(total)),
        "calculation": f"{len(duplicate_sets)} duplicate set(s) found",
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-104 §1", "POL-104 §3"],
        "duplicate_sets": duplicate_sets,
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_etf(contract_id: str, request_text: str) -> Dict:
    """Check early termination fee dispute eligibility per POL-105."""
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
            return {"eligible": False, "amount": "0.00", "calculation": "Contract not found",
                    "evidence_ids": [], "policy_sections": ["POL-105 §1", "POL-105 §2"],
                    "billed_etf": "0.00", "expected_etf": "0.00",
                    "waiver_a": False, "waiver_b": False, "waiver_c": False}

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
            return {"eligible": False, "amount": "0.00",
                    "calculation": "Missing termination or end date",
                    "evidence_ids": [], "policy_sections": ["POL-105 §1"],
                    "billed_etf": "0.00", "expected_etf": "0.00",
                    "waiver_a": False, "waiver_b": False, "waiver_c": False}

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

    return {
        "eligible": eligible,
        "amount": amount,
        "calculation": calc_str,
        "evidence_ids": evidence_ids,
        "policy_sections": ["POL-105 §1", "POL-105 §2"],
        "billed_etf": str(billed_etf),
        "expected_etf": str(expected_etf),
        "waiver_a": {"qualifies": waiver_a, "months_below_sla": months_below_sla},
        "waiver_b": {"qualifies": waiver_b, "sc_number": sc_number},
        "waiver_c": {"qualifies": waiver_c, "new_contract_id": new_contract_id},
    }


@tool(
    expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}]
)
def check_completeness(request_id: str) -> Dict:
    """Check whether an adjustment request has all POL-000 §3 required fields."""
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
    """Return the required approval level for a given credit amount per POL-001 §1."""
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
    """Insert a new adjustment request and return its generated request_id."""
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
    """Record an AI or human decision against an adjustment request."""
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
