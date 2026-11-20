"""Mock supplier API (SPEC Step 1).

search_offers(part_id) is the catalog: it shows listed_stock.
recheck_offer(offer_id) is the live call: it shows live_stock.
"""

import db


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
    return dict(row) if row else None
