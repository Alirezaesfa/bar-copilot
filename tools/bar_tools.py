"""Maplewave billing-adjustment tools — Presto/Iceberg backed."""

import os
from typing import List, Dict, Any

from dotenv import load_dotenv
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import tool

APP_ID = "maplewave_data"

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
def get_customer_contracts(customer_name: str) -> List[Dict[str, Any]]:
    """Return contracts for customers whose name contains the given text.

    Args:
        customer_name (str): Partial or full customer name to search for.

    Returns:
        List[Dict]: One dict per contract with fields: customer_name,
            contract_id, service_name, quantity, unit_price_cad,
            discount_pct, net_mrc_cad, status, sla_uptime_pct.
    """
    cfg = _get_cfg()
    cat = cfg["PRESTO_CATALOG"]
    sch = cfg["PRESTO_SCHEMA"]

    sql = f"""
        SELECT
            cu.customer_name,
            co.contract_id,
            s.service_name,
            co.quantity,
            co.unit_price_cad,
            co.discount_pct,
            co.net_mrc_cad,
            co.status,
            co.sla_uptime_pct
        FROM {cat}.{sch}.contracts  co
        JOIN {cat}.{sch}.customers  cu ON cu.customer_id = co.customer_id
        JOIN {cat}.{sch}.services   s  ON s.service_code  = co.service_code
        WHERE LOWER(cu.customer_name) LIKE ?
        ORDER BY cu.customer_name, co.contract_id
    """
    param = f"%{customer_name.lower()}%"

    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, [param])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()

    return [dict(zip(cols, row)) for row in rows]
