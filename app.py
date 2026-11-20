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
    .pb-table-wrap { max-height: 540px; overflow-y: auto; border: 1px solid rgba(128,128,128,.25); border-radius: 10px; }
    .pb-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 1.05rem; }
    .pb-table thead th { position: sticky; top: 0; z-index: 5; text-align: left; padding: 0.6rem 0.65rem; background: var(--secondary-background-color, #f0f2f6); border-bottom: 2px solid rgba(128,128,128,.5); }
    .pb-table td { padding: 0.55rem 0.65rem; border-bottom: 1px solid rgba(128,128,128,.22); vertical-align: top; }
    .pb-table td:first-child { min-width: 17rem; }
    .pb-table td.num, .pb-table thead th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pb-table td.avail { font-weight: 800; font-size: 1.2rem; }
    .pb-why { display: block; font-size: 0.92rem; opacity: 0.75; margin-top: 0.15rem; }
    .pb-pill { padding: 0.1rem 0.55rem; border-radius: 999px; font-weight: 700; font-size: 0.95rem; white-space: nowrap; }
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

if "part_limits" not in st.session_state:
    st.session_state["part_limits"] = dict(DEFAULT_PART_LIMITS)
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
        st.caption("No global limit. Only parts listed below require approval if exceeded.")
        current_limits = dict(st.session_state["part_limits"])
        to_remove = None
        for pid in sorted(current_limits.keys()):
            col_val, col_rm = st.columns([4, 1])
            with col_val:
                new_val = st.number_input(
                    f"{pid} {(_sidebar_part_names.get(pid) or '')[:18]} ($)".strip(),
                    min_value=0,
                    value=int(current_limits[pid]),
                    step=100,
                    key=f"limit_input_{pid}",
                )
                current_limits[pid] = float(new_val)
            with col_rm:
                st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
                if st.button("✕", key=f"rm_limit_{pid}", help=f"Remove limit for {pid}"):
                    to_remove = pid
        if to_remove:
            current_limits.pop(to_remove, None)
            st.session_state["part_limits"] = current_limits
            st.rerun()
        st.session_state["part_limits"] = current_limits

        unconfigured = [p for p in _sidebar_part_ids if p not in current_limits]
        if unconfigured:
            add_pid = st.selectbox(
                "Add limit for part",
                unconfigured,
                format_func=lambda p: f"{p} {_sidebar_part_names.get(p, '')}".strip(),
                key="add_limit_part",
            )
            add_c1, add_c2 = st.columns([2, 1.3])
            with add_c1:
                add_amt = st.number_input("Limit ($)", min_value=0, value=500, step=100, key="add_limit_amt")
            with add_c2:
                st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
                if st.button("Add", key="add_limit_btn", width="stretch"):
                    current_limits[add_pid] = float(add_amt)
                    st.session_state["part_limits"] = current_limits
                    st.rerun()

    active_part_limits = dict(st.session_state["part_limits"])

    mode = st.radio(
        "Mode", ["Agent", "Rules only"], horizontal=True,
        help="Agent: the AI shops and explains. Rules only: the same rules run without the AI.",
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
    st.session_state["part_limits"] = dict(DEFAULT_PART_LIMITS)
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
        # The crash switch is read by the backend from CRASH_AFTER_ORDERS (SPEC section 7).
        previous_crash = os.environ.get("CRASH_AFTER_ORDERS")
        os.environ["CRASH_AFTER_ORDERS"] = "2" if crash_test else "0"
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
tab_inv, tab_appr, tab_orders, tab_offers, tab_audit = st.tabs(
    ["Inventory", waiting_tab, "Orders", "Bulk & Suppliers", "Audit trail"]
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


with tab_inv:
    if not db_ready:
        st.info("Press Reset demo")
    elif backend_note("rules"):
        pass
    elif inventory is None:
        st.warning(f"backend not ready: rules ({inventory_error})")
    else:
        head = (
            "<thead><tr><th>Part</th><th class='num'>On hand</th><th class='num'>Reserved for jobs</th>"
            "<th class='num'>On order</th><th class='num'>Available</th><th class='num'>Reorder at</th>"
            "<th>Status</th></tr></thead>"
        )
        body = []
        for p in inventory:
            pill_class, pill_text = STATUS_PILLS.get(p["status"], ("", p["status"]))
            lim_badge = (
                f" · cap {money(active_part_limits[p['part_id']])}"
                if p["part_id"] in active_part_limits
                else ""
            )
            body.append(
                f"<tr><td><b>{p['part_id']}</b> {p['name']}<span class='pb-why'>{available_line(p)}{lim_badge}</span></td>"
                f"<td class='num'>{p['on_hand']}</td><td class='num'>{p['reserved']}</td>"
                f"<td class='num'>{p['on_order']}</td><td class='num avail'>{p['available']}</td>"
                f"<td class='num'>{p['reorder_point']}</td>"
                f"<td><span class='pb-pill {pill_class}'>{pill_text}</span></td></tr>"
            )
        st.markdown(
            f"<div class='pb-table-wrap'><table class='pb-table'>{head}<tbody>{''.join(body)}</tbody></table></div>",
            unsafe_allow_html=True,
        )
        st.caption("Available = on hand - reserved for jobs (header locked while scrolling).")


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

# Bulk & Suppliers
with tab_offers:
    if not db_ready:
        st.info("Press Reset demo")
    else:
        offers_df = read_table(
            "SELECT o.offer_id, o.part_id, o.supplier_id, s.name AS supplier_name, "
            "o.unit_price, COALESCE(o.min_order_qty, 1) AS min_order_qty, o.bulk_unit_price, "
            "s.shipping_flat, s.lead_time_days, o.listed_stock "
            "FROM offers o JOIN suppliers s ON s.supplier_id = o.supplier_id "
            "ORDER BY o.part_id, o.offer_id"
        )
        if offers_df is None or offers_df.empty:
            st.caption("No supplier offers loaded.")
        else:
            st.caption(
                "Minimum order batch (MOQ): when an offer has a minimum batch > 1, buying from that offer "
                "orders at least the minimum batch at the bulk unit price."
            )
            st.table(
                pd.DataFrame(
                    {
                        "Offer": offers_df["offer_id"],
                        "Part": [part_label(p) for p in offers_df["part_id"]],
                        "Supplier": [str(n).replace(" (mock)", "") for n in offers_df["supplier_name"]],
                        "Unit price": offers_df["unit_price"].map(money),
                        "Min batch (MOQ)": offers_df["min_order_qty"].astype(int),
                        "Bulk unit price": [
                            money(v) if pd.notna(v) else "—" for v in offers_df["bulk_unit_price"]
                        ],
                        "Shipping": offers_df["shipping_flat"].map(money),
                        "Lead time": [f"{int(d)}d" for d in offers_df["lead_time_days"]],
                        "Catalog stock": offers_df["listed_stock"].astype(int),
                    }
                ).set_index("Offer")
            )
            with st.expander("Edit an offer's minimum batch / bulk discount", expanded=False):
                offer_ids = list(offers_df["offer_id"])
                offer_lookup = offers_df.set_index("offer_id").to_dict("index")
                sel_oid = st.selectbox(
                    "Offer",
                    offer_ids,
                    format_func=lambda oid: (
                        f"{oid} — {part_label(offer_lookup[oid]['part_id'])} · "
                        f"{str(offer_lookup[oid]['supplier_name']).replace(' (mock)', '')}"
                    ),
                )
                cur = offer_lookup[sel_oid]
                e1, e2, e3 = st.columns([1.2, 1.4, 1])
                with e1:
                    new_moq = st.number_input(
                        "Min batch (MOQ)",
                        min_value=1,
                        value=int(cur["min_order_qty"] or 1),
                        step=1,
                        key=f"moq_edit_{sel_oid}",
                    )
                with e2:
                    cur_bulk = 0.0 if pd.isna(cur["bulk_unit_price"]) else float(cur["bulk_unit_price"])
                    new_bulk = st.number_input(
                        "Bulk unit price ($; 0 = none)",
                        min_value=0.0,
                        value=cur_bulk,
                        step=1.0,
                        key=f"bulk_edit_{sel_oid}",
                    )
                with e3:
                    st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
                    if st.button("Save offer", key=f"save_offer_{sel_oid}", width="stretch"):
                        try:
                            backend["db"].update_offer_bulk(sel_oid, int(new_moq), new_bulk if new_bulk > 0 else None)
                            st.session_state["flash"] = (
                                "success",
                                f"Updated {sel_oid}: min batch {int(new_moq)}"
                                + (f", bulk price {money(new_bulk)}" if new_bulk > 0 else " (no bulk discount)") + ".",
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not update {sel_oid}: {exc}")

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
            if pid in latest_order.index:
                o = latest_order.loc[pid]
                outcome = f"{STATUS_LABELS.get(o['status'], o['status'])}: {supplier_label(o['supplier_id'])}, {money(o['total'])}"
            elif (steps["step"] == "checked").any():
                outcome = "OK, nothing to buy"
            else:
                outcome = "No order"
            title = f"{part_label(pid)}  —  {outcome}" + ("  ⚠️ stale stock caught" if catches else "")
            with st.expander(title, expanded=bool(catches)):
                for d in catches:
                    st.warning(
                        f"**Caught before buying ({supplier_label(d.get('supplier_id'))}):** "
                        f"Catalog said {d['listed_stock']} in stock. Live check said {d['live_stock']}.",
                        icon="🔎",
                    )
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


