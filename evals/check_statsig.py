"""Statsig proof for the sponsor scorecard (SPEC A5b, test 15a).

  python evals/check_statsig.py           where the limit comes from right now
  python evals/check_statsig.py --flip    reset, run rules-only with the LIVE limit,
                                          report P06 and the order count
  python evals/check_statsig.py --watch   poll every 2 s; print how long a console
                                          change takes to arrive (target: under 60 s)

Needs USE_STATSIG=1 and STATSIG_SERVER_SECRET in .env. Never prints the secret.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_flags  # noqa: E402


def status():
    secret_set = bool(os.getenv("STATSIG_SERVER_SECRET", "").strip())
    value, source = config_flags.get_live_limit()
    print(f"USE_STATSIG={os.getenv('USE_STATSIG', '0')}  secret set: {'yes' if secret_set else 'NO'}")
    print(f"Spending limit: ${value:,.0f}   source: {source}")
    if source != "Statsig":
        print("Not reading Statsig. Check: USE_STATSIG=1, secret in .env, a Dynamic Config named "
              "'parts_buyer_rules' with a rule that returns {\"spend_limit\": <number>}.")
    return value, source


def flip():
    import db
    import rules

    value, source = status()
    db.reset_demo()
    summary = rules.run_rules_only(spend_limit=None)   # None = use the live limit
    with sqlite3.connect(db.DB_PATH) as conn:
        p06 = conn.execute("SELECT status, total FROM purchase_orders WHERE part_id = 'P06'").fetchone()
        placed = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status = 'placed'").fetchone()[0]
    print(f"Run used limit ${summary['spend_limit']:,.0f} ({source}).")
    if p06:
        print(f"P06 ${p06[1]:,.0f}: {'PLACED automatically' if p06[0] == 'placed' else 'waiting for approval'}")
    print(f"Orders placed: {placed}  (expected 3 at $1,000, 4 at $3,000)")


def watch():
    start_value, _ = status()
    print("Watching. Change spend_limit in the Statsig console now (Ctrl+C to stop).")
    t0 = time.time()
    try:
        while True:
            time.sleep(2)
            value, source = config_flags.get_live_limit()
            if value != start_value:
                print(f"Changed to ${value:,.0f} ({source}) after {time.time() - t0:.0f} s "
                      f"from the start of watching.")
                start_value, t0 = value, time.time()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flip", action="store_true")
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()
    flip() if a.flip else watch() if a.watch else status()
