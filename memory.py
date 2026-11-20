"""Cognee Cloud memory integration (SPEC section A5, sponsor 2).

Uses Cognee Cloud's HTTP API (/api/v1/remember, /api/v1/remember/entry,
/api/v1/recall, /api/v1/search) with X-Api-Key authentication via httpx.

Design rules:
- Memory only advises; rules.py and live rechecks always decide.
- Never raises: any connection/auth/quota error logs 'sponsor_error: cognee: ...'
  and falls back cleanly so purchasing runs never break.
- Never hardcode credentials; read COGNEE_API_URL and COGNEE_API_KEY from env.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

log = logging.getLogger("parts_buyer.memory")

POLICY_PATH = ROOT / "docs" / "policy.md"
PARTS_CSV_PATH = ROOT / "data" / "parts.csv"
DEFAULT_DATASET = "parts_buyer_memory"
DEFAULT_SESSION_ID = "parts_buyer"


def _load_part_names() -> dict[str, str]:
    names = {
        "P01": "Battery pack 14.4V",
        "P02": "Handpiece motor seal kit",
        "P03": "Drill chuck assembly",
        "P04": "Foot switch cable",
        "P05": "Camera head cable",
        "P06": "Light source lamp module",
        "P07": "Shaver blade hub",
        "P08": "Sterilization case latch",
    }
    if PARTS_CSV_PATH.exists():
        try:
            with PARTS_CSV_PATH.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    pid = (row.get("part_id") or "").strip()
                    pname = (row.get("name") or "").strip()
                    if pid and pname:
                        names[pid] = pname
        except Exception:
            pass
    return names


PART_NAMES = _load_part_names()

_lock = threading.Lock()
_policy_synced = False
_last_error: str | None = None


def _sponsor_error(reason: str) -> None:
    global _last_error
    if reason != _last_error:
        log.warning("sponsor_error: cognee: %s", reason)
        _last_error = reason


def _config() -> tuple[bool, str, str, str, str]:
    enabled = os.getenv("USE_COGNEE", "0").strip() == "1"
    base_url = os.getenv("COGNEE_API_URL", "").strip().rstrip("/")
    api_key = os.getenv("COGNEE_API_KEY", "").strip()
    tenant_id = os.getenv("COGNEE_TENANT_ID", "").strip()
    dataset = os.getenv("COGNEE_DATASET", DEFAULT_DATASET).strip() or DEFAULT_DATASET
    return enabled, base_url, api_key, tenant_id, dataset


def is_enabled() -> bool:
    enabled, base_url, api_key, _, _ = _config()
    return bool(enabled and base_url and api_key)


def _headers(api_key: str, tenant_id: str, *, json_body: bool = False) -> dict[str, str]:
    h: dict[str, str] = {"X-Api-Key": api_key}
    if tenant_id:
        h["X-Tenant-Id"] = tenant_id
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _http_client():
    import httpx

    return httpx


def check_connection(timeout: float = 3.5) -> tuple[bool, str]:
    """Quick connectivity check against Cognee Cloud. Never raises."""
    enabled, base_url, api_key, tenant_id, _ = _config()
    if not enabled:
        return False, "disabled (set USE_COGNEE=1)"
    if not base_url or not api_key:
        return False, "missing COGNEE_API_URL or COGNEE_API_KEY"
    try:
        httpx = _http_client()
        resp = httpx.get(
            f"{base_url}/api/v1/datasets",
            headers=_headers(api_key, tenant_id),
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            data = resp.json()
            count = len(data) if isinstance(data, list) else 0
            return True, f"Connected ({count} dataset{'s' if count != 1 else ''})"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        _sponsor_error(f"connection check failed ({type(exc).__name__})")
        return False, f"unreachable ({type(exc).__name__})"


def seed_policy(*, force: bool = False, background: bool = True, timeout: float = 15.0) -> dict[str, Any]:
    """Upload docs/policy.md to Cognee Cloud via POST /api/v1/remember. Never raises."""
    global _policy_synced
    if not is_enabled():
        return {"ok": False, "reason": "disabled"}
    with _lock:
        if _policy_synced and not force:
            return {"ok": True, "cached": True}

    _, base_url, api_key, tenant_id, dataset = _config()
    if not POLICY_PATH.exists():
        _sponsor_error("docs/policy.md not found")
        return {"ok": False, "reason": "policy.md missing"}

    try:
        httpx = _http_client()
        policy_bytes = POLICY_PATH.read_bytes()
        resp = httpx.post(
            f"{base_url}/api/v1/remember",
            headers=_headers(api_key, tenant_id),
            data={
                "datasetName": dataset,
                "run_in_background": "true" if background else "false",
            },
            files=[("data", ("policy.md", policy_bytes, "text/markdown"))],
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code in (200, 201, 202):
            with _lock:
                _policy_synced = True
            return {"ok": True, "status": resp.status_code, "dataset": dataset}
        _sponsor_error(f"seed_policy HTTP {resp.status_code}: {resp.text[:160]}")
        return {"ok": False, "status": resp.status_code, "reason": resp.text[:160]}
    except Exception as exc:
        _sponsor_error(f"seed_policy failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "reason": str(exc)}


def _ensure_period(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    return s if s[-1] in ".!?" else f"{s}."


def _humanize_item(raw_item: str, *, for_part: str | None = None) -> str:
    """Turn raw Cognee QA / audit snippets into clean, natural-English lines."""
    s = (raw_item or "").strip()
    if not s:
        return ""

    # Strip rule codes like "(R4)" or "R6: " and add commas to 4+ digit dollar figures
    s = re.sub(r"\s*\(R\d+\)", "", s)
    s = re.sub(r"\bR\d+\s*:\s*", "", s)
    s = re.sub(
        r"\$(\d{4,})(\.\d+)?",
        lambda m: "$" + f"{int(m.group(1)):,}" + (m.group(2) or ""),
        s,
    )

    # Fix missing period when two audit messages were joined with a space
    # e.g. "...needs it by Nov 22 Rechecked OEM Direct:" -> "...needs it by Nov 22. Rechecked OEM Direct:"
    s = re.sub(
        r"([A-Za-z0-9\)])\s+(Rechecked\s+|Placed\s+PO-|Held\s+PO-|Asked\s+owner|No\s+offer\s+meets|(?:Broker\s+[A-Z]|OEM\s+Direct)\s+(?:would\b|is\s+not\b))",
        r"\1. \2",
        s,
    )

    # Simplify repetitive live-recheck wording:
    # "Rechecked Broker A: catalog said 50 in stock at $28, live says 50 at $28."
    def _simplify_recheck(m: re.Match) -> str:
        sup, cat_qty, cat_p, live_qty, live_p = (
            m.group(1).strip(),
            m.group(2),
            m.group(3),
            m.group(4),
            m.group(5),
        )
        if cat_qty == live_qty and cat_p == live_p:
            return f"Live check confirmed {sup} ({live_qty} in stock at ${live_p})."
        return f"Live check on {sup}: catalog listed {cat_qty} at ${cat_p}, live showed {live_qty} at ${live_p}."

    s = re.sub(
        r"Rechecked\s+([^:]+):\s*catalog said\s+(\d+)\s+in stock at\s+\$([0-9,]+(?:\.[0-9]+)?),\s*live says\s+(\d+)\s+at\s+\$([0-9,]+(?:\.[0-9]+)?)\.?",
        _simplify_recheck,
        s,
    )

    # Smooth out "Total $295 (no spending cap on this part). 10 added to on order."
    s = re.sub(
        r"Total\s+(\$[0-9,]+(?:\.[0-9]+)?)\s*\(([^)]+)\)\.\s*(\d+)\s+added to on order\.?",
        r"\3 units, \1 total (\2).",
        s,
    )
    s = re.sub(r"(\d+)\s+added to on order\.?", r"(\1 units ordered).", s)
    s = s.replace("no spending cap on this part", "no spending cap")

    # Replace machine prefix: "Run run-20261120T233700-c19c54 lesson for P02: ..."
    m_prefix = re.match(
        r"^(?:Run\s+\S+\s+lesson\s+for\s+|Lesson\s+for\s+)(P\d{2})(?:\s*\([^)]*\))?\s*:\s*(.+)$",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_prefix:
        pid = m_prefix.group(1).upper()
        body = _ensure_period(m_prefix.group(2))
        if for_part and for_part.upper() == pid:
            return body
        pname = PART_NAMES.get(pid)
        label = f"**{pid} · {pname}**" if pname else f"**{pid}**"
        return f"{label} — {body}"

    # Also handle "**P02** (Name): ..." or "P02 (Name): ..." when recalling for a specific part
    m_part = re.match(
        r"^\*{0,2}(P\d{2})\*{0,2}(?:\s*[·\-]\s*[^:—]+|\s*\([^)]*\))?\s*[:—-]\s*(.+)$",
        s,
        flags=re.DOTALL,
    )
    if m_part:
        pid = m_part.group(1).upper()
        body = _ensure_period(m_part.group(2))
        if for_part and for_part.upper() == pid:
            return body
        pname = PART_NAMES.get(pid)
        label = f"**{pid} · {pname}**" if pname else f"**{pid}**"
        return f"{label} — {body}"

    return _ensure_period(s)


def _extract_items(payload: Any) -> list[str]:
    """Extract raw memory strings from Cognee /recall or /search responses."""
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        # Split legacy pipe-joined strings if present
        return [chunk.strip() for chunk in text.split(" | ") if chunk.strip()]
    if isinstance(payload, list):
        items: list[str] = []
        for entry in payload[:5]:
            for sub in _extract_items(entry):
                if sub not in items:
                    items.append(sub)
        return items
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            if key in payload and isinstance(payload[key], list):
                return _extract_items(payload[key])
        for key in ("text", "answer", "content", "summary", "search_result"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return _extract_items(val)
            if isinstance(val, list) and val:
                found = _extract_items(val)
                if found:
                    return found
    return []


def _extract_text(payload: Any, *, multiline: bool = False, for_part: str | None = None) -> str:
    """Normalize Cognee /recall or /search responses into clean, human-readable text."""
    raw_items = _extract_items(payload)
    if not raw_items:
        return ""
    cleaned: list[str] = []
    for item in raw_items[:4]:
        h = _humanize_item(item, for_part=for_part)
        if h and h not in cleaned:
            cleaned.append(h)
    if not cleaned:
        return ""
    if multiline:
        if len(cleaned) == 1:
            return cleaned[0]
        return "\n".join(f"- {line}" for line in cleaned)
    return " · ".join(cleaned)


def recall_for_part(
    part_id: str,
    part_name: str,
    *,
    context: str = "",
    timeout: float = 4.5,
) -> dict[str, Any] | None:
    """Recall policy clauses and prior lessons for a part from Cognee Cloud (<5s). Never raises."""
    if not is_enabled():
        return None
    _, base_url, api_key, tenant_id, dataset = _config()
    query = (
        f"Purchasing policy rules and past supplier lessons for {part_id} ({part_name}). "
        f"{context}".strip()
    )

    try:
        httpx = _http_client()
        # 1. Try /api/v1/recall (auto-routed session + graph lookup)
        resp = httpx.post(
            f"{base_url}/api/v1/recall",
            headers=_headers(api_key, tenant_id, json_body=True),
            json={
                "query": query,
                "datasets": [dataset],
                "session_id": DEFAULT_SESSION_ID,
                "top_k": 3,
            },
            timeout=timeout,
            follow_redirects=True,
        )
        # 2. If dataset doesn't exist yet (404) or session-only recall is needed, retry session-scoped recall
        if resp.status_code == 404:
            resp = httpx.post(
                f"{base_url}/api/v1/recall",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "query": query,
                    "session_id": DEFAULT_SESSION_ID,
                    "top_k": 3,
                },
                timeout=timeout,
                follow_redirects=True,
            )
        # 3. Fallback to /api/v1/search if /recall returned non-200
        if resp.status_code != 200:
            resp = httpx.post(
                f"{base_url}/api/v1/search",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "query": query,
                    "datasets": [dataset],
                    "search_type": "GRAPH_COMPLETION",
                    "top_k": 3,
                },
                timeout=timeout,
                follow_redirects=True,
            )

        if resp.status_code != 200:
            _sponsor_error(f"recall HTTP {resp.status_code}: {resp.text[:140]}")
            return None

        text = _extract_text(resp.json(), multiline=False, for_part=part_id)
        if not text:
            return None
        snippet = text[:420].rstrip() + ("…" if len(text) > 420 else "")
        return {"text": snippet, "dataset": dataset, "source": "cognee_cloud"}
    except Exception as exc:
        _sponsor_error(f"recall failed ({type(exc).__name__}: {exc})")
        return None


def remember_run_lessons(run_id: str, entries: list[dict[str, Any]], timeout: float = 8.0) -> dict[str, Any]:
    """Store key lessons (stale stock, rejections, approvals) from a completed run into Cognee Cloud."""
    if not is_enabled():
        return {"ok": False, "stored": 0, "reason": "disabled"}
    _, base_url, api_key, tenant_id, dataset = _config()

    by_part: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        pid = e.get("part_id")
        if pid and e.get("step") in (
            "rejected_option",
            "recheck",
            "order_placed",
            "approval_requested",
            "no_viable_offer",
        ):
            by_part.setdefault(pid, []).append(e)

    if not by_part:
        return {"ok": True, "stored": 0}

    stored = 0
    summary_lines = [f"# Purchasing Lessons ({run_id})"]
    try:
        httpx = _http_client()
        for pid, rows in sorted(by_part.items()):
            lesson_bits = [_ensure_period(r["message"]) for r in rows[:4] if r.get("message")]
            if not lesson_bits:
                continue
            pname = PART_NAMES.get(pid, "")
            part_header = f"{pid} ({pname})" if pname else pid
            raw_joined = " ".join(lesson_bits)
            clean_body = _humanize_item(raw_joined, for_part=pid)
            summary_lines.append(f"- **{part_header}**: {clean_body}")
            # Fast session entry so the next run immediately recalls this lesson
            r = httpx.post(
                f"{base_url}/api/v1/remember/entry",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "entry": {
                        "type": "qa",
                        "question": f"What happened on past runs for {part_header}?",
                        "answer": f"{part_header}: {clean_body}",
                    },
                    "dataset_name": dataset,
                    "session_id": DEFAULT_SESSION_ID,
                },
                timeout=timeout,
                follow_redirects=True,
            )
            if r.status_code in (200, 201, 202):
                stored += 1

        # Also enqueue markdown lesson summary into the knowledge graph asynchronously
        doc_bytes = ("\n".join(summary_lines) + "\n").encode("utf-8")
        httpx.post(
            f"{base_url}/api/v1/remember",
            headers=_headers(api_key, tenant_id),
            data={"datasetName": dataset, "run_in_background": "true"},
            files=[("data", (f"lessons_{run_id}.md", doc_bytes, "text/markdown"))],
            timeout=timeout,
            follow_redirects=True,
        )
        return {"ok": True, "stored": stored, "dataset": dataset}
    except Exception as exc:
        _sponsor_error(f"remember_run_lessons failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "stored": stored, "reason": str(exc)}


def inspect_memory(timeout: float = 5.0) -> dict[str, Any]:
    """Return Cognee Cloud connection state, datasets, and documents (no secrets). Never raises."""
    enabled, base_url, api_key, tenant_id, dataset = _config()
    info: dict[str, Any] = {
        "enabled": bool(enabled and base_url and api_key),
        "flag_on": enabled,
        "connected": False,
        "status": "",
        "dataset": dataset,
        "session_id": DEFAULT_SESSION_ID,
        "api_url": base_url,
        "policy_synced": _policy_synced,
        "last_error": _last_error,
        "datasets": [],
        "documents": [],
    }
    if not enabled:
        info["status"] = "Disabled (set USE_COGNEE=1)"
        return info
    if not base_url or not api_key:
        info["status"] = "Missing COGNEE_API_URL or COGNEE_API_KEY"
        return info

    try:
        httpx = _http_client()
        resp = httpx.get(
            f"{base_url}/api/v1/datasets",
            headers=_headers(api_key, tenant_id),
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            info["status"] = f"HTTP {resp.status_code}: {resp.text[:120]}"
            return info

        raw = resp.json()
        raw_list = raw if isinstance(raw, list) else (raw.get("datasets") or raw.get("data") or [])
        datasets: list[dict[str, Any]] = []
        target_dataset_id: str | None = None
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            ds_name = str(item.get("name") or item.get("datasetName") or "")
            ds_id = str(item.get("id") or item.get("dataset_id") or "")
            datasets.append(
                {
                    "name": ds_name,
                    "id": ds_id,
                    "created_at": str(item.get("created_at") or item.get("createdAt") or ""),
                }
            )
            if ds_name == dataset and ds_id:
                target_dataset_id = ds_id

        info["connected"] = True
        info["datasets"] = datasets
        count = len(datasets)
        info["status"] = f"Connected ({count} dataset{'s' if count != 1 else ''})"

        # Best-effort fetch of documents inside the configured dataset
        if target_dataset_id:
            try:
                doc_resp = httpx.get(
                    f"{base_url}/api/v1/datasets/{target_dataset_id}/data",
                    headers=_headers(api_key, tenant_id),
                    timeout=timeout,
                    follow_redirects=True,
                )
                if doc_resp.status_code == 200:
                    doc_raw = doc_resp.json()
                    doc_list = doc_raw if isinstance(doc_raw, list) else (doc_raw.get("data") or [])
                    docs: list[dict[str, Any]] = []
                    for d in doc_list:
                        if not isinstance(d, dict):
                            continue
                        docs.append(
                            {
                                "name": str(d.get("name") or d.get("raw_data_location") or d.get("id") or ""),
                                "type": str(d.get("mime_type") or d.get("extension") or ""),
                                "created_at": str(d.get("created_at") or d.get("createdAt") or ""),
                            }
                        )
                    info["documents"] = docs
            except Exception:
                pass
        return info
    except Exception as exc:
        _sponsor_error(f"inspect failed ({type(exc).__name__})")
        info["status"] = f"Unreachable ({type(exc).__name__}: {exc})"
        return info


def query_memory(query: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Run an ad-hoc memory recall/search query against Cognee Cloud for inspection. Never raises."""
    cleaned = (query or "").strip()
    if not cleaned:
        return {"ok": False, "text": "", "error": "Enter a question to query Cognee."}
    if not is_enabled():
        return {"ok": False, "text": "", "error": "Cognee Cloud is disabled (set USE_COGNEE=1, COGNEE_API_URL, COGNEE_API_KEY)."}

    _, base_url, api_key, tenant_id, dataset = _config()
    t0 = time.perf_counter()
    endpoint = "/api/v1/recall"
    try:
        httpx = _http_client()
        resp = httpx.post(
            f"{base_url}/api/v1/recall",
            headers=_headers(api_key, tenant_id, json_body=True),
            json={
                "query": cleaned,
                "datasets": [dataset],
                "session_id": DEFAULT_SESSION_ID,
                "top_k": 3,
            },
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code == 404:
            endpoint = "/api/v1/recall (session)"
            resp = httpx.post(
                f"{base_url}/api/v1/recall",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "query": cleaned,
                    "session_id": DEFAULT_SESSION_ID,
                    "top_k": 3,
                },
                timeout=timeout,
                follow_redirects=True,
            )
        if resp.status_code != 200:
            endpoint = "/api/v1/search"
            resp = httpx.post(
                f"{base_url}/api/v1/search",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "query": cleaned,
                    "datasets": [dataset],
                    "search_type": "GRAPH_COMPLETION",
                    "top_k": 3,
                },
                timeout=timeout,
                follow_redirects=True,
            )

        elapsed_s = round(time.perf_counter() - t0, 2)
        if resp.status_code != 200:
            return {
                "ok": False,
                "text": "",
                "endpoint": endpoint,
                "status_code": resp.status_code,
                "elapsed_s": elapsed_s,
                "dataset": dataset,
                "raw": resp.text[:400],
                "error": f"HTTP {resp.status_code}: {resp.text[:180]}",
            }

        payload = resp.json()
        text = _extract_text(payload, multiline=True)
        if len(text) > 1500:
            text = text[:1500].rstrip() + "…"
        return {
            "ok": True,
            "text": text,
            "endpoint": endpoint,
            "status_code": resp.status_code,
            "elapsed_s": elapsed_s,
            "dataset": dataset,
            "raw": payload,
            "error": None,
        }
    except Exception as exc:
        elapsed_s = round(time.perf_counter() - t0, 2)
        _sponsor_error(f"query_memory failed ({type(exc).__name__}: {exc})")
        return {
            "ok": False,
            "text": "",
            "endpoint": endpoint,
            "status_code": None,
            "elapsed_s": elapsed_s,
            "dataset": dataset,
            "raw": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

