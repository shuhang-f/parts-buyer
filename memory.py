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

import logging
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

log = logging.getLogger("parts_buyer.memory")

POLICY_PATH = ROOT / "docs" / "policy.md"
DEFAULT_DATASET = "parts_buyer_memory"
DEFAULT_SESSION_ID = "parts_buyer"

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


def _extract_text(payload: Any) -> str:
    """Normalize Cognee /recall or /search responses into concise readable text."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload[:3]:
            t = _extract_text(item)
            if t:
                parts.append(t)
        return " | ".join(parts)
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            if key in payload and isinstance(payload[key], list):
                return _extract_text(payload[key])
        for key in ("text", "answer", "content", "summary", "search_result"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                joined = _extract_text(val)
                if joined:
                    return joined
    return ""


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
            )

        if resp.status_code != 200:
            _sponsor_error(f"recall HTTP {resp.status_code}: {resp.text[:140]}")
            return None

        text = _extract_text(resp.json())
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
    summary_lines = [f"# Run lessons ({run_id})"]
    try:
        httpx = _http_client()
        for pid, rows in sorted(by_part.items()):
            lesson_bits = [r["message"] for r in rows[:4] if r.get("message")]
            if not lesson_bits:
                continue
            lesson_text = " ".join(lesson_bits)
            summary_lines.append(f"- **{pid}**: {lesson_text}")
            # Fast session entry so the next run immediately recalls this lesson
            r = httpx.post(
                f"{base_url}/api/v1/remember/entry",
                headers=_headers(api_key, tenant_id, json_body=True),
                json={
                    "entry": {
                        "type": "qa",
                        "question": f"What happened on past runs for {pid}?",
                        "answer": f"Run {run_id} lesson for {pid}: {lesson_text}",
                    },
                    "dataset_name": dataset,
                    "session_id": DEFAULT_SESSION_ID,
                },
                timeout=timeout,
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
        )
        return {"ok": True, "stored": stored, "dataset": dataset}
    except Exception as exc:
        _sponsor_error(f"remember_run_lessons failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "stored": stored, "reason": str(exc)}
