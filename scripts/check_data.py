"""Check connectivity to watsonx.data and print row counts for all tables."""

import os
import sys
from dotenv import load_dotenv
import prestodb

load_dotenv(override=True)

REQUIRED = ["WXD_USER", "WXD_API_KEY", "PRESTO_HOST", "PRESTO_PORT", "PRESTO_CATALOG", "PRESTO_SCHEMA"]

TABLES = [
    "services",
    "promotions",
    "employees",
    "customers",
    "contracts",
    "invoices",
    "invoice_lines",
    "service_outages",
    "adjustment_requests",
    "review_decisions",
]


def main():
    """Connect to Presto and print row counts for every table."""
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("Check your .env file and make sure all required variables are set.")
        sys.exit(1)

    host = os.environ["PRESTO_HOST"]
    port = int(os.environ["PRESTO_PORT"])
    catalog = os.environ["PRESTO_CATALOG"]
    schema = os.environ["PRESTO_SCHEMA"]
    user = os.environ["WXD_USER"]
    # key loaded but never printed
    api_key = os.environ["WXD_API_KEY"]

    # watsonx.data SaaS LDAP auth requires the prefix "ibmlhapikey_<user>"
    ldap_user = user if user.startswith("ibmlhapikey_") else f"ibmlhapikey_{user}"

    print(f"Connecting to {host}:{port}  catalog={catalog}  schema={schema}")

    try:
        conn = prestodb.dbapi.connect(
            host=host,
            port=port,
            user=ldap_user,
            catalog=catalog,
            schema=schema,
            http_scheme="https",
            auth=prestodb.auth.BasicAuthentication(ldap_user, api_key),
        )
        cur = conn.cursor()
    except Exception as exc:
        print(f"ERROR: could not create connection — {exc}")
        print("Check PRESTO_HOST, PRESTO_PORT, WXD_USER, WXD_API_KEY.")
        sys.exit(1)

    print(f"\n{'Table':<25} {'Row count':>10}")
    print("-" * 37)
    all_ok = True
    for table in TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {catalog}.{schema}.{table}")
            row = cur.fetchone()
            count = row[0] if row else 0
            print(f"{table:<25} {count:>10,}")
        except Exception as exc:
            print(f"{table:<25} {'ERROR':>10}  ({exc})")
            all_ok = False

    conn.close()
    print()
    if all_ok:
        print("All tables reachable.")
    else:
        print("Some tables failed — check the errors above.")


if __name__ == "__main__":
    main()
