"""Mock supplier API (SPEC Step 1).

search_offers(part_id) is the catalog: it shows listed_stock.
recheck_offer(offer_id) is the live call: it shows live_stock.
"""

import os

import db

# Simulated live supplier price shifts when SIMULATE_PRICE_CHANGE=1 (demo test switch)
SIMULATED_LIVE_PRICES = {
    "O1": 195.0,    # P01 OEM Direct: $175 -> $195
    "O4": 31.0,     # P02 Broker A:   $28  -> $31
    "O6": 105.0,    # P04 OEM Direct: $92  -> $105
    "O9": 1240.0,   # P06 Broker A:   $1190 -> $1240
}


def search_offers(part_id):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT o.offer_id, o.part_id, o.supplier_id, s.name AS supplier_name,
                   s.country, s.lead_time_days, s.shipping_flat,
                   o.unit_price, o.listed_stock,
                   COALESCE(o.min_order_qty, 1) AS min_order_qty,
                   o.bulk_unit_price
            FROM offers o JOIN suppliers s ON s.supplier_id = o.supplier_id
            WHERE o.part_id = ?
            ORDER BY o.offer_id
            """,
            (part_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recheck_offer(offer_id):
    """Live stock and price for one offer, or None if the offer does not exist."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT offer_id, live_stock, unit_price, "
            "COALESCE(min_order_qty, 1) AS min_order_qty, bulk_unit_price "
            "FROM offers WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()
    conn.close()
    if row is None:
        return None
    out = dict(row)
    if os.getenv("SIMULATE_PRICE_CHANGE", "0").strip() == "1" and offer_id in SIMULATED_LIVE_PRICES:
        out["unit_price"] = SIMULATED_LIVE_PRICES[offer_id]
    return out
