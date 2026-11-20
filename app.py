"""Parts Buyer screens (Streamlit).

Display only. All money and compliance logic lives in rules.py; this file
reads the database, shows it, and calls the backend when a button is pressed.
"""

import inspect
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB_PATH = ROOT / "parts_buyer.db"
TODAY = os.getenv("TODAY", "2026-11-20")
USE_STATSIG = os.getenv("USE_STATSIG", "0").strip() == "1"
LOCAL_TZ = datetime.now().astimezone().tzinfo

st.set_page_config(page_title="Parts Buyer", page_icon="🔧", layout="wide")

# Projector sizing: bigger base text, numbers, tabs and tables.
st.markdown(
    """
    <style>
    html { font-size: 19px; }
    .stMarkdown p, .stMarkdown li, .stAlert p { font-size: 1.12rem; line-height: 1.5; }
    [data-testid="stMetricValue"] { font-size: 2.4rem; font-weight: 700; }
    [data-testid="stMetricLabel"] p { font-size: 1.05rem; }
    [data-testid="stTab"] p, button[data-baseweb="tab"] p { font-size: 1.3rem; font-weight: 600; }
    [data-testid="stExpander"] summary p { font-size: 1.15rem; font-weight: 600; }
    [data-testid="stTable"] td, [data-testid="stTable"] th { font-size: 1.05rem; }
    .pb-summary { font-size: 1.6rem; font-weight: 700; margin: 0.2rem 0 0.8rem; }
    .pb-table-wrap { overflow-x: auto; border: 1px solid rgba(128,128,128,.25); border-radius: 10px; }
    .pb-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 1.02rem; }
    .pb-table thead th { text-align: left; padding: 0.6rem 0.65rem; background: #1f2430; color: #f5f7fa; border-bottom: 2px solid rgba(148,163,184,.45); }
    .pb-table td { padding: 0.55rem 0.65rem; border-bottom: 1px solid rgba(128,128,128,.22); vertical-align: top; }
    .pb-table td:first-child { min-width: 14rem; }
    .pb-table td.num, .pb-table thead th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pb-table td.avail { font-weight: 800; font-size: 1.2rem; }
    .pb-inv-head { background: #1f2430; color: #f5f7fa; border-radius: 8px; padding: 0.5rem 0.7rem; font-weight: 700; font-size: 0.96rem; margin-bottom: 0.35rem; }
    .pb-inv-head-grid { display: grid; grid-template-columns: 3.8fr 0.85fr 0.95fr 0.85fr 0.95fr 1fr 1.4fr; gap: 0.5rem; align-items: center; }
    .pb-inv-head-grid .num { text-align: right; }
    .pb-cell-num { text-align: right; font-variant-numeric: tabular-nums; padding-top: 0.35rem; font-size: 1.04rem; }
    .pb-cell-avail { text-align: right; font-variant-numeric: tabular-nums; padding-top: 0.28rem; font-weight: 800; font-size: 1.2rem; }
    .pb-cell-status { padding-top: 0.3rem; }
    .pb-row-sep { border: none; border-top: 1px solid rgba(128,128,128,.2); margin: 0.25rem 0 0.35rem; }
    .pb-cap-badge { display: inline-block; margin-left: 0.35rem; padding: 0.05rem 0.45rem; border-radius: 999px; font-size: 0.82rem; font-weight: 700; background: rgba(230,160,0,.22); color: #d99b00; }
    .pb-why { display: block; font-size: 0.9rem; opacity: 0.75; margin-top: 0.05rem; }
    .pb-pill { padding: 0.1rem 0.55rem; border-radius: 999px; font-weight: 700; font-size: 0.92rem; white-space: nowrap; }
    .pb-ok { background: rgba(33,150,83,.18); color: #1b7f46; }
    .pb-short { background: rgba(214,69,65,.16); color: #b3261e; }
    .pb-ordered { background: rgba(30,110,220,.16); color: #1a5fb4; }
    .pb-waiting { background: rgba(230,160,0,.22); color: #8a5a00; }
    .pb-queued { background: rgba(128,128,128,.14); color: rgba(128,128,128,.95); }
    .pb-checking { background: rgba(30,110,220,.18); color: #1a5fb4; outline: 2px solid rgba(30,110,220,.55); }
    .pb-tracker { display: flex; flex-wrap: wrap; gap: 0.45rem; margin: 0.45rem 0 0.75rem; }
    .pb-track-item { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.22rem 0.6rem; border-radius: 999px; font-size: 0.88rem; font-weight: 600; }
    .pb-meta { font-size: 0.92rem; opacity: 0.75; }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- Backend modules. Missing ones show a note instead of crashing.
backend = {}
missing = []
for _name in ("db", "rules", "agent"):
    try:
        backend[_name] = __import__(_name)
    except Exception as exc:  # ImportError, or an error inside a half-built module
        missing.append((_name, exc))


def backend_note(name):
    """Yellow note for a module that is not there yet. Returns True if missing."""
    if name in backend:
        return False
    st.warning(f"backend not ready: {name}")
    return True


# --- Read-only database helpers --------------------------------------------
def read_table(sql, params=()):
    """Return a DataFrame, or None if the database or table is not there yet."""
    if not DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return None


def db_usable():
    """True only if the database file exists and its parts table can be read."""
    parts = read_table("SELECT part_id FROM parts LIMIT 1")
    return parts is not None and not parts.empty


def money(value):
    try:
        return f"${float(value):,.0f}" if float(value).is_integer() else f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0"


def local_time(ts, fmt="%-I:%M:%S %p"):
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert(LOCAL_TZ)
        return t.strftime(fmt)
    except (ValueError, TypeError):
        return str(ts or "")


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


STEP_LABELS = {
    "checked": "Checked",
    "gap_found": "Needs buying",
    "shopping": "Compared offers",
    "rejected_option": "Ruled out",
    "chosen": "Chosen",
    "recheck": "Live check",
    "order_placed": "Order placed",
    "approval_requested": "Asked you to approve",
    "approved": "You approved",
    "rejected": "You rejected",
    "refused": "Order blocked",
    "skipped": "Already handled",
    "no_viable_offer": "No offer works",
    "agent_note": "Agent's explanation",
    "recalled": "Recalled from memory",
}


def plain_english(text):
    """Presentation only: drop rule codes, add commas to dollars, swap jargon for plain words."""
    text = str(text or "")
    text = re.sub(r"\s*\(R\d+\)", "", text)          # "... misses due date (R4)"
    text = re.sub(r"\bR\d+\s*:\s*", "", text)         # "R6: $301 under ..."
    text = re.sub(
        r"\$(\d{4,})(\.\d+)?",
        lambda m: "$" + f"{int(m.group(1)):,}" + (m.group(2) or ""),
        text,
    )
    text = text.replace("lowest landed cost", "lowest total cost including shipping")
    text = text.replace("landed cost", "total cost including shipping")
    text = re.sub(r"(\d) x \$", r"\1 × $", text)
    text = re.sub(                                    # 2026-11-27 -> Nov 27
        r"\b(\d{4})-(\d{2})-(\d{2})\b",
        lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%b %-d"),
        text,
    )
    return text


DEFAULT_PART_LIMITS = {"P01": 800, "P06": 1000}


def default_today_date():
    try:
        return date.fromisoformat(TODAY)
    except ValueError:
        return date(2026, 11, 20)


# Pre-read part names for sidebar selectors
_sidebar_parts = read_table("SELECT part_id, name FROM parts ORDER BY part_id")
_sidebar_part_names = (
    dict(zip(_sidebar_parts["part_id"], _sidebar_parts["name"]))
    if _sidebar_parts is not None
    else {"P01": "Battery pack 14.4V", "P06": "Light source lamp module"}
)
_sidebar_part_ids = list(_sidebar_part_names.keys()) or ["P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08"]

if "part_limit_values" not in st.session_state:
    st.session_state["part_limit_values"] = dict(DEFAULT_PART_LIMITS)
if "selected_limit_parts" not in st.session_state:
    st.session_state["selected_limit_parts"] = sorted(DEFAULT_PART_LIMITS.keys())
if "selected_date" not in st.session_state:
    st.session_state["selected_date"] = default_today_date()

# --- Sidebar ---------------------------------------------------------------
with st.sidebar:
    st.title("Parts Buyer")
    st.caption("Your shop's purchasing agent, with receipts")
    selected_date = st.date_input(
        "Today",
        value=st.session_state["selected_date"],
        help="Changes delivery deadline checks and the daily order key (advance the date to simulate a new day).",
    )
    st.session_state["selected_date"] = selected_date
    os.environ["TODAY"] = selected_date.isoformat()

    with st.container(border=True):
        st.markdown("**Part spending limits**")
        active_part_limits = {
            pid: float(st.session_state["part_limit_values"].get(pid, 500))
            for pid in sorted(st.session_state["selected_limit_parts"])
            if pid in _sidebar_part_ids
        }
        if active_part_limits:
            chips = "".join(
                f"<span class='pb-cap-badge' style='margin:0.15rem 0.25rem 0.15rem 0;'>"
                f"{pid}: {money(val)}</span>"
                for pid, val in active_part_limits.items()
            )
            st.markdown(chips, unsafe_allow_html=True)
            st.caption("Click any part in **Inventory** to edit or remove its cap.")
            if st.button("Clear all caps", key="clear_all_caps", width="stretch"):
                st.session_state["selected_limit_parts"] = []
                st.rerun()
        else:
            st.caption("No part caps set. Click any part in **Inventory** to add a spending limit.")

    mode = st.radio(
        "Mode", ["Agent", "Rules only"], horizontal=True,
        help="Agent: the AI shops and explains. Rules only: the same rules run without the AI.",
    )
    simulate_price_change = st.toggle(
        "Simulate live price change",
        help="Simulates suppliers changing live prices during the scan so Buy price updates in Pricing.",
    )
    crash_test = st.toggle("Crash test", help="Stops the run on purpose after 2 orders, to show a restart never orders twice.")
    run_clicked = st.button("Run agent", type="primary", width="stretch")
    reset_clicked = st.button("Reset demo", width="stretch")

    for _name, _exc in missing:
        st.warning(f"backend not ready: {_name}")

if reset_clicked:
    st.session_state.pop("ran", None)
    st.session_state.pop("last_result", None)
    st.session_state.pop("last_activity", None)
    st.session_state.pop("last_part_states", None)
    st.session_state["part_limit_values"] = dict(DEFAULT_PART_LIMITS)
    st.session_state["selected_limit_parts"] = sorted(DEFAULT_PART_LIMITS.keys())
    st.session_state["selected_date"] = default_today_date()
    os.environ["TODAY"] = default_today_date().isoformat()
    if "db" in backend:
        try:
            backend["db"].reset_demo()
            st.session_state["flash"] = ("success", "Demo reset. Fresh stock, no orders.")
        except Exception as exc:
            st.session_state["flash"] = ("error", f"Reset failed: {exc}")
    else:
        st.session_state["flash"] = ("warning", "backend not ready: db")

# --- Lookup helpers & live KPI/tracker renderers ---------------------------
names = read_table("SELECT part_id, name FROM parts ORDER BY part_id")
part_names = dict(zip(names["part_id"], names["name"])) if names is not None else {}
all_part_ids = list(part_names.keys())
sups = read_table("SELECT supplier_id, name FROM suppliers")
supplier_names = dict(zip(sups["supplier_id"], sups["name"])) if sups is not None else {}


def supplier_label(sid):
    return str(supplier_names.get(sid, sid)).replace(" (mock)", "")


def part_label(pid):
    return f"{pid} {part_names.get(pid, '')}".strip()


def row_data(row):
    try:
        return json.loads(row["data_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def compute_kpis(orders_df, audit_df, active_run_id=None):
    if orders_df is not None and not orders_df.empty:
        placed = orders_df[orders_df["status"] == "placed"]
        n_placed = len(placed)
        committed = float(placed["total"].fillna(0).sum())
        n_awaiting = int((orders_df["status"] == "pending_approval").sum())
    else:
        n_placed, committed, n_awaiting = 0, 0.0, 0

    runs_df = (
        audit_df[~audit_df["run_id"].astype(str).str.startswith("owner-")]
        if audit_df is not None and not audit_df.empty
        else None
    )
    if runs_df is not None and not runs_df.empty:
        target_run = active_run_id or runs_df["run_id"].iloc[-1]
        n_checked = int(runs_df.loc[runs_df["run_id"] == target_run, "part_id"].dropna().nunique())
    else:
        n_checked = 0
    return n_checked, n_placed, committed, n_awaiting


def read_live_kpis(active_run_id=None):
    o = read_table("SELECT total, status FROM purchase_orders")
    a = read_table("SELECT run_id, part_id FROM audit_log ORDER BY ts, id")
    return compute_kpis(o, a, active_run_id=active_run_id)


def render_kpis(slot, n_checked, n_placed, committed, n_awaiting):
    with slot.container():
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Parts checked", n_checked)
        c2.metric("Orders placed", n_placed)
        c3.metric("Dollars committed", money(committed))
        c4.metric("Awaiting approval", n_awaiting)


TRACK_STYLE = {
    "queued": ("pb-queued", "Queued"),
    "checking": ("pb-checking", "Checking…"),
    "ok": ("pb-ok", "Stock OK"),
    "ordered": ("pb-ordered", "Ordered"),
    "waiting": ("pb-waiting", "Awaiting approval"),
    "skipped": ("pb-queued", "Handled"),
    "blocked": ("pb-short", "Blocked"),
}


def render_tracker(slot, part_states):
    if not part_states:
        slot.empty()
        return
    pills = []
    for pid, state_key in part_states.items():
        css, label = TRACK_STYLE.get(state_key, ("pb-queued", state_key))
        pills.append(f"<span class='pb-track-item {css}'><b>{pid}</b> · {label}</span>")
    slot.markdown(f"<div class='pb-tracker'>{''.join(pills)}</div>", unsafe_allow_html=True)


# --- Top slots (updated live during a run) ---------------------------------
flash_slot = st.empty()
summary_slot = st.empty()
kpi_slot = st.empty()

init_checked, init_placed, init_dollars, init_awaiting = read_live_kpis()
render_kpis(kpi_slot, init_checked, init_placed, init_dollars, init_awaiting)

live_ran_this_pass = False
if run_clicked:
    module = "agent" if mode == "Agent" else "rules"
    if not db_usable():
        st.session_state["flash"] = ("warning", "Press Reset demo first. The demo data isn't loaded yet.")
    elif module not in backend:
        st.session_state["flash"] = ("warning", f"backend not ready: {module}")
    else:
        # The crash and price-change switches are read by the backend from env vars.
        previous_crash = os.environ.get("CRASH_AFTER_ORDERS")
        previous_price_sim = os.environ.get("SIMULATE_PRICE_CHANGE")
        os.environ["CRASH_AFTER_ORDERS"] = "2" if crash_test else "0"
        os.environ["SIMULATE_PRICE_CHANGE"] = "1" if simulate_price_change else "0"
        live_ran_this_pass = True
        try:
            if module == "agent":
                part_states = {pid: "queued" for pid in all_part_ids}
                total_parts = max(len(all_part_ids), 1)
                done_parts = set()
                activity_lines = []
                active_run_ref = {"run_id": None}

                progress_bar = st.progress(0.0, text="Starting Parts Buyer agent…")
                tracker_slot = st.empty()
                render_tracker(tracker_slot, part_states)
                status_box = st.status("Running Parts Buyer agent…", expanded=True)

                def add_log(line):
                    activity_lines.append(line)
                    status_box.markdown(line)

                def refresh_live_ui(text=None):
                    frac = min(len(done_parts) / total_parts, 1.0)
                    label = text or f"{len(done_parts)} / {total_parts} parts checked"
                    progress_bar.progress(frac, text=label)
                    render_tracker(tracker_slot, part_states)
                    render_kpis(kpi_slot, *read_live_kpis(active_run_ref["run_id"]))

                def on_event(ev):
                    etype = ev.get("type")
                    pid = ev.get("part_id")
                    if etype == "run_started":
                        active_run_ref["run_id"] = ev.get("run_id")
                        for p in ev.get("part_ids") or []:
                            part_states.setdefault(p, "queued")
                        model_txt = ev.get("model") or "model"
                        pl = ev.get("part_limits")
                        if pl is not None:
                            lim_txt = (
                                ", ".join(f"{k}: {money(v)}" for k, v in sorted(pl.items()))
                                if pl
                                else "no part caps"
                            )
                        else:
                            lim_txt = money(ev.get("spend_limit", 1000))
                        add_log(
                            f"🚀 **Started run** (`{model_txt}`) · date **{ev.get('today', os.environ.get('TODAY'))}** · limits: **{lim_txt}**"
                        )
                        refresh_live_ui(f"0 / {total_parts} parts checked · Connected to {model_txt}")

                    elif etype == "thinking":
                        turn = ev.get("turn", 1)
                        model_txt = ev.get("model") or "model"
                        progress_bar.progress(
                            min(len(done_parts) / total_parts, 0.99),
                            text=f"{len(done_parts)} / {total_parts} parts checked · Thinking (step {turn}, {model_txt})…",
                        )

                    elif etype == "inventory_checked":
                        tot = ev.get("total_parts", total_parts)
                        short = ev.get("short_count", 0)
                        add_log(f"📋 **Checked inventory** — {tot} parts total ({short} short, {tot - short} OK)")

                    elif etype == "part_start" and pid:
                        part_states[pid] = "checking"
                        refresh_live_ui(f"{len(done_parts)} / {total_parts} parts checked · Inspecting {part_label(pid)}…")

                    elif etype == "part_evaluated" and pid:
                        if ev.get("existing_order"):
                            po = ev["existing_order"]
                            part_states[pid] = "skipped"
                            done_parts.add(pid)
                            add_log(f"⏭️ **{part_label(pid)}** — Already handled today ({po.get('po_id')})")
                        elif not ev.get("needs_buying"):
                            part_states[pid] = "ok"
                            done_parts.add(pid)
                            add_log(
                                f"✅ **{part_label(pid)}** — Stock OK "
                                f"({ev.get('available')} available, reorder at {ev.get('reorder_point')})"
                            )
                        else:
                            part_states[pid] = "checking"
                            bits = [
                                f"🔎 **{part_label(pid)}** — Short "
                                f"({ev.get('available')} available < {ev.get('reorder_point')}), need **{ev.get('qty')}**"
                            ]
                            rejected = ev.get("rejected") or []
                            if rejected:
                                rej_txt = "; ".join(
                                    f"Ruled out {r.get('supplier_name') or supplier_label(r.get('supplier_id'))}"
                                    for r in rejected[:2]
                                )
                                bits.append(rej_txt)
                            eligible = ev.get("eligible_offers") or []
                            if eligible:
                                best = eligible[0]
                                moq_tag = (
                                    f", bulk batch {best.get('qty')}"
                                    if best.get("bulk_applied")
                                    else (f", min batch {best.get('qty')}" if best.get("min_order_qty", 1) > 1 else "")
                                )
                                bits.append(
                                    f"Best eligible: **{best.get('supplier_name') or supplier_label(best.get('supplier_id'))}** "
                                    f"({money(best.get('landed'))}{moq_tag})"
                                )
                            else:
                                part_states[pid] = "blocked"
                                done_parts.add(pid)
                                bits.append("No eligible supplier found")
                            add_log(" · ".join(bits))
                        refresh_live_ui()

                    elif etype == "offer_rechecked":
                        sup = supplier_label(ev.get("supplier_id"))
                        tag = f"**{pid}**" if pid else f"**{ev.get('offer_id')}**"
                        if ev.get("passed"):
                            if ev.get("price_changed"):
                                add_log(
                                    f"💲 {tag} — Live scan updated buy price for **{sup}**: "
                                    f"{money(ev.get('listed_price'))} → **{money(ev.get('live_price'))}** "
                                    f"({ev.get('live_stock')} in stock)"
                                )
                            else:
                                add_log(
                                    f"🔎 {tag} — Live check passed for **{sup}** "
                                    f"({ev.get('live_stock')} in stock at {money(ev.get('live_price'))})"
                                )
                        else:
                            add_log(
                                f"⚠️ {tag} — Live check failed for **{sup}** "
                                f"({plain_english(ev.get('reason'))}) → trying next supplier"
                            )

                    elif etype == "order_result" and pid:
                        st_val = ev.get("status")
                        sup = supplier_label(ev.get("supplier_id"))
                        po_id = ev.get("po_id")
                        tot = ev.get("total")
                        lim = ev.get("spend_limit")
                        bulk_tag = f" · bulk batch {ev.get('qty')}" if ev.get("bulk_applied") else ""
                        if st_val == "placed":
                            part_states[pid] = "ordered"
                            done_parts.add(pid)
                            lim_note = f"within {money(lim)} limit" if lim is not None else "no spending cap"
                            add_log(
                                f"🛒 **{part_label(pid)}** — Placed **{po_id}** with **{sup}** "
                                f"({money(tot)}, {lim_note}{bulk_tag})"
                            )
                        elif st_val == "pending_approval":
                            part_states[pid] = "waiting"
                            done_parts.add(pid)
                            add_log(
                                f"⏸️ **{part_label(pid)}** — Over {money(lim)} limit for {pid} ({money(tot)}{bulk_tag}) "
                                f"→ Held **{po_id}** with **{sup}** for owner approval"
                            )
                        elif st_val == "exists":
                            part_states[pid] = "skipped"
                            done_parts.add(pid)
                            add_log(f"⏭️ **{part_label(pid)}** — {plain_english(ev.get('reason'))}")
                        else:
                            part_states[pid] = "blocked"
                            done_parts.add(pid)
                            add_log(f"🛑 **{part_label(pid)}** — Order blocked: {plain_english(ev.get('reason'))}")
                        refresh_live_ui()

                    elif etype == "memory_recalled" and pid:
                        txt = str(ev.get("text") or "")
                        short_txt = txt[:160] + ("…" if len(txt) > 160 else "")
                        add_log(f"🧠 **{pid}** — Recalled from Cognee memory: _{short_txt}_")

                    elif etype == "memory_saved":
                        add_log(f"🧠 Saved **{ev.get('stored', 0)}** run lessons to Cognee Cloud memory")

                    elif etype == "reasoning_logged" and pid:
                        add_log(f"💬 **{pid}** — Saved plain-English explanation")

                    elif etype == "nudge":
                        rem = ", ".join((ev.get("missing_parts") or []) + (ev.get("unfinished_parts") or []))
                        add_log(f"🔁 Finishing remaining parts: **{rem}**")

                    elif etype == "safety_net":
                        rem = ", ".join((ev.get("missing_parts") or []) + (ev.get("unfinished_parts") or []))
                        add_log(f"🛡️ Completing remaining parts via rules engine: **{rem}**")

                    elif etype == "run_finished":
                        summ = ev.get("summary") or {}
                        elapsed = summ.get("elapsed_seconds", 0)
                        n_c, n_p, d_c, n_a = read_live_kpis(active_run_ref["run_id"])
                        render_kpis(kpi_slot, n_c, n_p, d_c, n_a)
                        progress_bar.progress(
                            1.0,
                            text=f"Done in {elapsed}s — {len(done_parts)} / {total_parts} parts checked",
                        )
                        render_tracker(tracker_slot, part_states)
                        status_box.update(
                            label=(
                                f"Agent run finished in {elapsed}s — "
                                f"{plural(n_p, 'order', 'orders')} placed ({money(d_c)}), "
                                f"{plural(n_a, 'order', 'orders')} awaiting approval"
                            ),
                            state="complete",
                            expanded=False,
                        )

                sig = inspect.signature(backend["agent"].run_agent)
                kwargs = {}
                if "on_event" in sig.parameters:
                    kwargs["on_event"] = on_event
                if "part_limits" in sig.parameters:
                    kwargs["part_limits"] = active_part_limits
                result = backend["agent"].run_agent(**kwargs)
                st.session_state["last_activity"] = activity_lines
                st.session_state["last_part_states"] = part_states
            else:
                status_box = None
                with st.status("Running rules engine…", expanded=False) as status_box:
                    r_sig = inspect.signature(backend["rules"].run_rules_only)
                    if "part_limits" in r_sig.parameters:
                        result = backend["rules"].run_rules_only(part_limits=active_part_limits)
                    else:
                        result = backend["rules"].run_rules_only()
                    status_box.update(label="Rules run finished.", state="complete", expanded=False)
                st.session_state.pop("last_activity", None)
                st.session_state.pop("last_part_states", None)
            st.session_state["ran"] = True
            st.session_state["last_result"] = result
        except Exception as exc:
            st.session_state.pop("ran", None)
            if module == "agent":
                st.session_state["last_activity"] = activity_lines
                st.session_state["last_part_states"] = part_states
            if "simulated crash" in str(exc).lower():
                if module == "agent":
                    status_box.markdown("💥 **Crash test triggered** — stopped on purpose after 2 orders. Everything saved so far is kept.")
                    status_box.update(label="Crash test: stopped on purpose after 2 orders", state="error", expanded=True)
                st.session_state["flash"] = (
                    "error",
                    "Crash test: the run stopped on purpose. Everything saved so far is kept. "
                    "Turn Crash test off and run again: nothing will be ordered twice.",
                )
            else:
                if module == "agent":
                    status_box.markdown(f"❌ **Run error:** {exc}")
                    status_box.update(label=f"Run stopped with an error", state="error", expanded=True)
                st.session_state["flash"] = ("error", f"The run stopped with an error: {exc}")
        finally:
            if previous_crash is None:
                os.environ.pop("CRASH_AFTER_ORDERS", None)
            else:
                os.environ["CRASH_AFTER_ORDERS"] = previous_crash
            if previous_price_sim is None:
                os.environ.pop("SIMULATE_PRICE_CHANGE", None)
            else:
                os.environ["SIMULATE_PRICE_CHANGE"] = previous_price_sim

# --- Data (fresh after any run/reset) --------------------------------------
db_ready = db_usable()
orders = read_table(
    "SELECT po_id, idempotency_key, part_id, supplier_id, offer_id, qty, unit_price, "
    "shipping, total, status, approved_by, created_at FROM purchase_orders"
)
audit = read_table(
    "SELECT id, run_id, ts, part_id, step, message, data_json FROM audit_log ORDER BY ts, id"
)
names = read_table("SELECT part_id, name FROM parts ORDER BY part_id")
part_names = dict(zip(names["part_id"], names["name"])) if names is not None else {}
sups = read_table("SELECT supplier_id, name FROM suppliers")
supplier_names = dict(zip(sups["supplier_id"], sups["name"])) if sups is not None else {}

inventory, inventory_error = None, None
if db_ready and "rules" in backend:
    try:
        inventory = backend["rules"].get_inventory_status()
    except Exception as exc:
        inventory_error = exc

parts_checked, orders_placed, dollars, awaiting = compute_kpis(orders, audit)
render_kpis(kpi_slot, parts_checked, orders_placed, dollars, awaiting)

# --- Top messages & summary ------------------------------------------------
flash = st.session_state.pop("flash", None)
if flash:
    with flash_slot.container():
        getattr(st, flash[0])(flash[1])
elif not db_ready:
    flash_slot.info("Press Reset demo")

# One-line run summary: "3 orders placed ($1,321), 1 needs your approval, 4 parts OK."
if st.session_state.get("ran") and db_ready:
    ok_parts = sum(1 for p in (inventory or []) if p.get("status") == "OK")
    if awaiting == 0:
        approval_text = "nothing needs your approval"
    else:
        approval_text = f"{awaiting} {'needs' if awaiting == 1 else 'need'} your approval"
    summary_slot.markdown(
        f'<div class="pb-summary">✅ {plural(orders_placed, "order", "orders")} placed '
        f"({money(dollars)}), {approval_text}, {plural(ok_parts, 'part', 'parts')} OK.</div>",
        unsafe_allow_html=True,
    )

waiting_tab = f"Approvals ({awaiting})" if awaiting else "Approvals"
tab_inv, tab_appr, tab_orders, tab_pricing, tab_sims, tab_audit = st.tabs(
    ["Inventory", waiting_tab, "Orders", "Pricing", "Simulations", "Audit trail"]
)

# Inventory
STATUS_PILLS = {
    "OK": ("pb-ok", "OK"),
    "Short": ("pb-short", "Short"),
    "Ordered": ("pb-ordered", "Ordered"),
    "Awaiting approval": ("pb-waiting", "Awaiting approval"),
}


def available_line(p):
    """'2 on hand - 2 reserved = 0, short.' Wording follows available = on_hand - reserved."""
    sums = f"{p['on_hand']} on hand - {p['reserved']} reserved = {p['available']}"
    if p["on_order"]:
        sums += f" ({p['on_order']} on order)"
    verdict = {
        "OK": "enough.",
        "Short": f"short: you want at least {p['reorder_point']}.",
        "Ordered": "ordered today.",
        "Awaiting approval": "short; an order is waiting for your approval.",
    }.get(p["status"], f"{p['status'].lower()}.")
    return f"{sums}, {verdict}"


@st.dialog("Part spending limit")
def edit_part_limit_dialog(pid):
    name = part_names.get(pid, pid)
    st.markdown(f"#### {pid} · {name}")
    currently_capped = pid in st.session_state["selected_limit_parts"]
    enable_cap = st.toggle(
        "Require owner approval above spending limit",
        value=currently_capped,
        key=f"dlg_toggle_{pid}",
    )
    cur_val = int(st.session_state["part_limit_values"].get(pid, 500))
    new_val = cur_val
    if enable_cap:
        new_val = st.number_input(
            "Spending limit per order ($)",
            min_value=0,
            value=cur_val,
            step=100,
            key=f"dlg_val_{pid}",
        )
    else:
        st.caption("No cap — eligible orders for this part will be placed automatically.")

    c_save, c_cancel = st.columns(2)
    with c_save:
        if st.button("Save", type="primary", width="stretch", key=f"dlg_save_{pid}"):
            selected = list(st.session_state["selected_limit_parts"])
            if enable_cap:
                if pid not in selected:
                    selected.append(pid)
                st.session_state["part_limit_values"][pid] = float(new_val)
            else:
                selected = [x for x in selected if x != pid]
            st.session_state["selected_limit_parts"] = sorted(selected)
            st.rerun()
    with c_cancel:
        if st.button("Cancel", width="stretch", key=f"dlg_cancel_{pid}"):
            st.rerun()


with tab_inv:
    if not db_ready:
        st.info("Press Reset demo")
    elif backend_note("rules"):
        pass
    elif inventory is None:
        st.warning(f"backend not ready: rules ({inventory_error})")
    else:
        st.caption("Click any part name below to set or edit its spending limit. Available = on hand - reserved for jobs.")
        st.markdown(
            "<div class='pb-inv-head'><div class='pb-inv-head-grid'>"
            "<div>Part</div><div class='num'>On hand</div><div class='num'>Reserved</div>"
            "<div class='num'>On order</div><div class='num'>Available</div>"
            "<div class='num'>Reorder at</div><div>Status</div>"
            "</div></div>",
            unsafe_allow_html=True,
        )
        col_widths = [3.8, 0.85, 0.95, 0.85, 0.95, 1.0, 1.4]
        for idx, p in enumerate(inventory):
            pid = p["part_id"]
            pill_class, pill_text = STATUS_PILLS.get(p["status"], ("", p["status"]))
            lim_badge = (
                f"<span class='pb-cap-badge'>Cap {money(active_part_limits[pid])}</span>"
                if pid in active_part_limits
                else ""
            )
            r_cols = st.columns(col_widths, vertical_alignment="top")
            with r_cols[0]:
                if st.button(
                    f"**{pid}** {p['name']}",
                    key=f"inv_pick_{pid}",
                    type="tertiary",
                    help="Click to set or edit this part's spending limit",
                ):
                    edit_part_limit_dialog(pid)
                st.markdown(
                    f"<span class='pb-why'>{available_line(p)}{lim_badge}</span>",
                    unsafe_allow_html=True,
                )
            with r_cols[1]:
                st.markdown(f"<div class='pb-cell-num'>{p['on_hand']}</div>", unsafe_allow_html=True)
            with r_cols[2]:
                st.markdown(f"<div class='pb-cell-num'>{p['reserved']}</div>", unsafe_allow_html=True)
            with r_cols[3]:
                st.markdown(f"<div class='pb-cell-num'>{p['on_order']}</div>", unsafe_allow_html=True)
            with r_cols[4]:
                st.markdown(f"<div class='pb-cell-avail'>{p['available']}</div>", unsafe_allow_html=True)
            with r_cols[5]:
                st.markdown(f"<div class='pb-cell-num'>{p['reorder_point']}</div>", unsafe_allow_html=True)
            with r_cols[6]:
                st.markdown(
                    f"<div class='pb-cell-status'><span class='pb-pill {pill_class}'>{pill_text}</span></div>",
                    unsafe_allow_html=True,
                )
            if idx < len(inventory) - 1:
                st.markdown("<hr class='pb-row-sep'/>", unsafe_allow_html=True)


# Approvals
def decide(action, po_id, description):
    """Button callback: runs before the page redraws, so the numbers refresh at once."""
    try:
        getattr(backend["rules"], f"{action}_order")(po_id)
        if action == "approve":
            st.session_state["flash"] = ("success", f"Approved: {description}. The order is placed.")
        else:
            st.session_state["flash"] = ("info", f"Rejected: {description}. Nothing was ordered.")
    except Exception as exc:
        st.session_state["flash"] = ("error", f"Could not {action} that order: {exc}")


with tab_appr:
    pending = (
        orders[orders["status"] == "pending_approval"].sort_values("created_at")
        if orders is not None and not orders.empty
        else None
    )
    if not db_ready:
        st.info("Press Reset demo")
    elif pending is None or pending.empty:
        st.success("Nothing waiting on you.")
    else:
        rules_ready = not backend_note("rules")
        for _, po in pending.iterrows():
            pid = po["part_id"]
            part_log = audit[audit["part_id"] == pid] if audit is not None else pd.DataFrame()
            asked = part_log[part_log["step"] == "approval_requested"] if not part_log.empty else pd.DataFrame()
            limit_then = row_data(asked.iloc[-1]).get("spend_limit") if len(asked) else None
            if limit_then is None:
                limit_then = active_part_limits.get(pid, 0)
            limit_then = float(limit_then)
            over = float(po["total"]) - limit_then
            notes = part_log[part_log["step"] == "agent_note"]["message"] if not part_log.empty else []
            chosen = part_log[part_log["step"] == "chosen"]["message"] if not part_log.empty else []
            reason = notes.iloc[-1] if len(notes) else chosen.iloc[-1] if len(chosen) else "No reason was logged."
            supplier = supplier_label(po["supplier_id"])
            description = f"{part_label(pid)} from {supplier}, {money(po['total'])}"

            with st.container(border=True):
                st.markdown(f"### {part_names.get(pid, pid)}")
                left, right = st.columns([3, 2])
                with left:
                    st.markdown(
                        f"**Part:** {pid} · {int(po['qty'])} ordered  \n"
                        f"**Supplier:** {supplier}  \n"
                        f"**Total:** {money(po['total'])} "
                        f"({int(po['qty'])} × {money(po['unit_price'])} + {money(po['shipping'])} shipping)"
                    )
                with right:
                    st.warning(f"**{money(over)} over your {money(limit_then)} limit for {pid}**")
                st.markdown(f"**Why this one:** {plain_english(reason)}")
                a, r, _ = st.columns([1.3, 1, 3])
                a.button(f"Approve {money(po['total'])}", key=f"approve_{po['po_id']}", type="primary",
                         disabled=not rules_ready, width="stretch",
                         on_click=decide, args=("approve", po["po_id"], description))
                r.button("Reject", key=f"reject_{po['po_id']}", disabled=not rules_ready, width="stretch",
                         on_click=decide, args=("reject", po["po_id"], description))

# Orders
STATUS_LABELS = {"placed": "Placed", "pending_approval": "Waiting for you", "rejected": "Rejected by you"}

with tab_orders:
    if not db_ready:
        st.info("Press Reset demo")
    elif orders is None:
        st.warning("backend not ready: purchase orders table")
    elif orders.empty:
        st.caption("No orders yet. Press Run agent.")
    else:
        po_table = orders.sort_values(["created_at", "po_id"], ascending=False)

        def approved_by(row):
            if row["status"] == "pending_approval":
                return "—"
            if row["approved_by"]:
                return str(row["approved_by"]).capitalize()
            if row["status"] == "placed":
                return "Auto (under part limit)" if row["part_id"] in active_part_limits else "Auto (no limit)"
            return ""

        st.table(
            pd.DataFrame(
                {
                    "Order #": po_table["po_id"],
                    "Part": [part_label(p) for p in po_table["part_id"]],
                    "Supplier": [supplier_label(s) for s in po_table["supplier_id"]],
                    "Qty": po_table["qty"],
                    "Total": po_table["total"].map(money),
                    "Status": po_table["status"].map(lambda s: STATUS_LABELS.get(s, s)),
                    "Approved by": [approved_by(r) for _, r in po_table.iterrows()],
                    "Daily order ID": po_table["idempotency_key"],
                }
            ).set_index("Order #")
        )
        st.caption("Daily order ID: one order per part per day, so a retry or restart can never order twice.")

# Pricing (Buy price from agent scan + Sell price)
with tab_pricing:
    if not db_ready:
        st.info("Press Reset demo")
    else:
        prices_df = read_table("SELECT part_id, name, buy_price, sell_price FROM parts ORDER BY part_id")
        if prices_df is None or prices_df.empty:
            st.caption("No pricing data loaded. Press Reset demo.")
        else:
            scanned_changes = {}
            if audit is not None and not audit.empty:
                for _, r in audit[audit["step"] == "recheck"].iterrows():
                    d = row_data(r)
                    if d.get("passed") and d.get("price_changed") and r["part_id"]:
                        scanned_changes[r["part_id"]] = (d.get("listed_price"), d.get("live_price"))

            head = (
                "<thead><tr><th>Part</th><th class='num'>Buy price</th>"
                "<th class='num'>Sell price</th></tr></thead>"
            )
            body = []
            for _, row in prices_df.iterrows():
                pid = row["part_id"]
                buy_v = float(row["buy_price"] or 0)
                sell_v = float(row["sell_price"] or 0)
                sub_note = ""
                if pid in scanned_changes:
                    old_p, new_p = scanned_changes[pid]
                    sub_note = f"<span class='pb-why'>Updated by live scan ({money(old_p)} → {money(new_p)})</span>"
                elif buy_v > 0 and sell_v > 0 and buy_v >= sell_v:
                    sub_note = "<span class='pb-why' style='color:#d64541;opacity:1;'>Buy price meets or exceeds sell price</span>"
                body.append(
                    f"<tr><td><b>{pid}</b> {row['name']}{sub_note}</td>"
                    f"<td class='num avail'>{money(buy_v)}</td>"
                    f"<td class='num avail'>{money(sell_v)}</td></tr>"
                )
            st.markdown(
                f"<div class='pb-table-wrap'><table class='pb-table'>{head}<tbody>{''.join(body)}</tbody></table></div>",
                unsafe_allow_html=True,
            )
            st.caption(
                "Buy price updates automatically from the agent's live supplier scan. "
                "Toggle 'Simulate live price change' in the sidebar to test live price updates."
            )

            st.markdown("##### Edit sell price")
            price_lookup = prices_df.set_index("part_id").to_dict("index")
            p1, p2, p3 = st.columns([2.2, 1.3, 1])
            with p1:
                edit_pid = st.selectbox(
                    "Part",
                    list(prices_df["part_id"]),
                    format_func=lambda p: f"{p} {price_lookup[p]['name']}",
                    key="edit_sell_part",
                )
            with p2:
                new_sell = st.number_input(
                    "Sell price ($)",
                    min_value=0.0,
                    value=float(price_lookup[edit_pid]["sell_price"] or 0),
                    step=5.0,
                    key=f"edit_sell_val_{edit_pid}",
                )
            with p3:
                st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
                if st.button("Save price", key=f"save_sell_{edit_pid}", width="stretch"):
                    try:
                        backend["db"].update_part_sell_price(edit_pid, new_sell)
                        st.session_state["flash"] = (
                            "success",
                            f"Updated sell price for {part_label(edit_pid)} to {money(new_sell)}.",
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not update sell price: {exc}")

# Simulations (Firedrill hard-rule drills)
with tab_sims:
    st.markdown("#### Firedrill Simulation & Hard-Rule Scorecard")
    st.caption(
        "Runs isolated stress-test worlds (never touches your live demo DB) and scores every run against "
        "hard rules: **R3** approved suppliers, **R5** live stock recheck, **R6** part spending caps, and **R7** zero duplicates. "
        "Also wired via `firedrill.recipe.json` + `scripts/firedrill_adapter.py` for Firedrill CLI / MCP."
    )
    try:
        import simulations as _sims

        s_c1, s_c2 = st.columns([2, 1.4])
        with s_c1:
            sim_mode_label = st.radio(
                "Simulation target",
                ["Rules engine (fast, deterministic)", "Live AI Agent (calls LLM)"],
                horizontal=True,
                key="sim_mode_radio",
            )
            sim_mode = "agent" if "Agent" in sim_mode_label else "rules"
        with s_c2:
            st.markdown("<div style='height:1.45rem'></div>", unsafe_allow_html=True)
            run_sims_clicked = st.button(
                f"Run all {len(_sims.SCENARIOS)} drills",
                type="primary",
                width="stretch",
                key="run_firedrill_suite",
            )

        if run_sims_clicked:
            with st.status("Running Firedrill simulation suite…", expanded=True) as sim_status:
                sim_results = []
                sim_prog = st.progress(0.0, text="Starting simulations…")
                total_drills = len(_sims.SCENARIOS)
                for idx, sc in enumerate(_sims.SCENARIOS, start=1):
                    sim_prog.progress((idx - 1) / total_drills, text=f"Running {idx}/{total_drills}: {sc['name']}…")
                    res = _sims.run_drill(sc["id"], mode=sim_mode)
                    sim_results.append(res)
                    icon = "✅" if res["passed"] else "❌"
                    sim_status.markdown(f"{icon} **{res['name']}** ({res['elapsed_ms']} ms)")
                passed_n = sum(1 for r in sim_results if r["passed"])
                viol_n = sum(len(r["hard_rule_violations"]) for r in sim_results)
                sim_prog.progress(1.0, text=f"Finished {passed_n}/{total_drills} scenarios")
                sim_status.update(
                    label=f"Simulation suite complete — {passed_n}/{total_drills} passed, {viol_n} hard-rule violations",
                    state="complete" if (passed_n == total_drills and viol_n == 0) else "error",
                    expanded=False,
                )
                st.session_state["last_sim_report"] = {
                    "mode": sim_mode,
                    "total_scenarios": total_drills,
                    "passed_scenarios": passed_n,
                    "pass_rate_pct": round(100.0 * passed_n / max(total_drills, 1), 1),
                    "hard_rule_violations": viol_n,
                    "elapsed_ms": round(sum(r["elapsed_ms"] for r in sim_results), 1),
                    "results": sim_results,
                }

        sim_report = st.session_state.get("last_sim_report")
        if sim_report:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Scenarios passed", f"{sim_report['passed_scenarios']}/{sim_report['total_scenarios']}")
            m2.metric("Pass rate", f"{sim_report['pass_rate_pct']}%")
            m3.metric("Hard-rule violations", sim_report["hard_rule_violations"])
            m4.metric("Runtime", f"{sim_report['elapsed_ms']} ms")
            for r in sim_report["results"]:
                badge = "✅ PASS" if r["passed"] else "❌ FAIL"
                rules_tag = ", ".join(r["rules"])
                with st.expander(f"{badge} · {r['name']}  ({rules_tag}) — {r['elapsed_ms']} ms", expanded=not r["passed"]):
                    st.caption(r["description"])
                    for chk in r["checks"]:
                        st.markdown(f"{'✅' if chk['pass'] else '❌'} {chk['detail']}")
                    if r["hard_rule_violations"]:
                        for v in r["hard_rule_violations"]:
                            st.error(v)
                    else:
                        st.success("0 hard-rule violations (R3, R5, R6, R7 verified)")
        else:
            st.info("Click **Run all 7 drills** above to test stale stock, landed cost, deadlines, caps, live price spikes, retries, and crash recovery.")
    except Exception as exc:
        st.warning(f"Simulations module unavailable: {exc}")

# Audit trail
with tab_audit:
    if not db_ready:
        st.info("Press Reset demo")
    elif audit is None:
        st.warning("backend not ready: audit log table")
    elif audit.empty:
        st.caption("No steps yet. Press Run agent.")
    else:
        part_ids = sorted(audit["part_id"].dropna().unique())
        choice = st.selectbox(
            "Show part",
            ["All parts"] + part_ids,
            format_func=lambda p: p if p == "All parts" else part_label(p),
        )
        shown = part_ids if choice == "All parts" else [choice]
        latest_order = (
            orders.sort_values("created_at").groupby("part_id").tail(1).set_index("part_id")
            if orders is not None and not orders.empty
            else pd.DataFrame()
        )
        for pid in shown:
            steps = audit[audit["part_id"] == pid]
            catches = [
                d for d in (row_data(r) for _, r in steps[steps["step"] == "recheck"].iterrows())
                if d.get("passed") is False and d.get("listed_stock") is not None
                and d.get("live_stock") is not None and d["live_stock"] < d["listed_stock"]
            ]
            recalled_rows = steps[steps["step"] == "recalled"]
            if pid in latest_order.index:
                o = latest_order.loc[pid]
                outcome = f"{STATUS_LABELS.get(o['status'], o['status'])}: {supplier_label(o['supplier_id'])}, {money(o['total'])}"
            elif (steps["step"] == "checked").any():
                outcome = "OK, nothing to buy"
            else:
                outcome = "No order"
            title = (
                f"{part_label(pid)}  —  {outcome}"
                + ("  ⚠️ stale stock caught" if catches else "")
                + ("  🧠 memory" if not recalled_rows.empty else "")
            )
            with st.expander(title, expanded=bool(catches)):
                for d in catches:
                    st.warning(
                        f"**Caught before buying ({supplier_label(d.get('supplier_id'))}):** "
                        f"Catalog said {d['listed_stock']} in stock. Live check said {d['live_stock']}.",
                        icon="🔎",
                    )
                for _, mem_row in recalled_rows.iterrows():
                    st.info(f"**Recalled from Cognee memory:** {mem_row['message']}", icon="🧠")
                show_details = st.toggle("Show raw data", key=f"details_{pid}")
                for _, row in steps.iterrows():
                    label = STEP_LABELS.get(row["step"], str(row["step"]).replace("_", " ").capitalize())
                    st.markdown(
                        f"**{label}** · {plain_english(row['message'])}  \n:gray[{local_time(row['ts'])}]"
                    )
                    if show_details:
                        st.json(row_data(row), expanded=False)

if st.session_state.get("last_result"):
    result = st.session_state["last_result"]
    if isinstance(result, dict):
        with st.container(border=True):
            st.markdown("#### Last run summary")
            placed_ids = result.get("placed") or []
            pending_ids = result.get("pending_approval") or []
            ok_ids = result.get("checked") or []
            skipped_ids = result.get("skipped") or []
            no_offer_ids = result.get("no_viable_offer") or []
            bullets = []
            if placed_ids:
                bullets.append(
                    f"- **Ordered automatically ({len(placed_ids)}):** "
                    + ", ".join(part_label(p) for p in placed_ids)
                )
            if pending_ids:
                bullets.append(
                    f"- **Waiting for your approval ({len(pending_ids)}):** "
                    + ", ".join(part_label(p) for p in pending_ids)
                )
            if no_offer_ids:
                bullets.append(
                    f"- **Short, no eligible supplier ({len(no_offer_ids)}):** "
                    + ", ".join(part_label(p) for p in no_offer_ids)
                )
            if ok_ids:
                bullets.append(
                    f"- **Stock OK ({len(ok_ids)}):** "
                    + ", ".join(part_label(p) for p in ok_ids)
                )
            if skipped_ids:
                bullets.append(
                    f"- **Already handled today ({len(skipped_ids)}):** "
                    + ", ".join(part_label(p) for p in skipped_ids)
                )
            if bullets:
                st.markdown("\n".join(bullets))
            meta_bits = []
            mode_label = "Agent" if result.get("mode") == "agent" else "Rules only"
            meta_bits.append(f"Mode: {mode_label}")
            if result.get("today"):
                meta_bits.append(f"Date: {result['today']}")
            if result.get("model"):
                meta_bits.append(f"Model: `{result['model']}`")
            if result.get("elapsed_seconds") is not None:
                meta_bits.append(f"Time: {result['elapsed_seconds']}s")
            pl = result.get("part_limits")
            if pl is not None:
                lim_str = ", ".join(f"{k}: {money(v)}" for k, v in sorted(pl.items())) if pl else "None"
                meta_bits.append(f"Part limits: {lim_str}")
            elif result.get("spend_limit") is not None:
                meta_bits.append(f"Spending limit: {money(result['spend_limit'])}")
            st.markdown(f"<span class='pb-meta'>{' · '.join(meta_bits)}</span>", unsafe_allow_html=True)

    saved_activity = st.session_state.get("last_activity")
    if saved_activity and not live_ran_this_pass:
        with st.expander(f"Step-by-step activity log ({len(saved_activity)} events)", expanded=False):
            for line in saved_activity:
                st.markdown(line)

    with st.expander("Technical details of the last run", expanded=False):
        if isinstance(result, (dict, list)):
            st.json(result)
        else:
            st.write(result)


