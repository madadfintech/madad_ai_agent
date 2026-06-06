"""End-to-end staging demo driver — walks the agent through every step of
the onboarding journey over HTTP and reports per-step status.

Drives the workflow service running on http://localhost:8001 (or any host
specified via --base-url) through:

  1. campaign/start            → campaign_await
  2. YES                        → existing-user check_contact branch →
                                  channel_session_first → consent_cr await
  3. CR attachment              → eligibility intake await
  4. eligibility form (defaults filled by the workflow if empty)
                                → financials await
  5. financial report           → buyers await
  6. buyer info                 → shareholders await
  7. shareholders               → documents await
  8. document attachments       → journey_wait_await
  9. status_update event        → payment_send_link → payment_await
 10. payment.completed event    → lender_wait_await
 11. final status_update        → offer_handoff terminal

For every step it prints the step name, the resulting prompt the
workflow is now waiting on, and a summary at the end. Designed for
staging where some tools (KYC upload, WhatsApp send, payment send-link)
are upstream-blocked: those failures are absorbed by the workflow's
staging-tolerant try/except and the demo proceeds.

Usage (inside the workflow container)::

    docker compose -f docker/docker-compose.yml --env-file .env exec workflow \\
        python -m scripts.staging_demo_runner \\
            --identity tech.external1@madadfintech.com \\
            --channel email

Outside the container (against the exposed port)::

    python -m scripts.staging_demo_runner \\
        --base-url http://34.18.50.97:8001 \\
        --identity tech.external1@madadfintech.com \\
        --channel email
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from typing import Any

import httpx
import jwt as pyjwt

from app.core.config import settings


def _hmac_sign(raw: bytes) -> str | None:
    sec = settings.security
    if not sec.webhook_secret:
        return None
    import hashlib
    import hmac

    return hmac.new(sec.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()


def _post(
    client: httpx.Client, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    # Webhook chokepoint requires HMAC over the raw body; other endpoints
    # accept it harmlessly when not validated.
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = _hmac_sign(raw)
    if sig and "/workflow/madad/events/" in path:
        header_name = settings.security.webhook_signature_header
        headers[header_name] = sig
        headers["Content-Type"] = "application/json"
        r = client.post(path, content=raw, headers=headers, timeout=60.0)
    else:
        r = client.post(path, json=body, timeout=60.0)
    r.raise_for_status()
    data: dict[str, Any] = r.json()
    return data


def _step(
    client: httpx.Client,
    name: str,
    path: str,
    body: dict[str, Any],
    *,
    expected_prompt_step: str | None = None,
) -> tuple[bool, dict[str, Any], float]:
    start = time.perf_counter()
    try:
        out = _post(client, path, body)
    except httpx.HTTPStatusError as exc:
        ms = (time.perf_counter() - start) * 1000
        return (
            False,
            {
                "error": f"HTTP {exc.response.status_code}",
                "body": exc.response.text[:200],
            },
            ms,
        )
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - start) * 1000
        return False, {"error": repr(exc)[:200]}, ms
    ms = (time.perf_counter() - start) * 1000
    prompt = (out.get("prompt") or {}).get("step") if isinstance(out, dict) else None
    ok = (
        expected_prompt_step is None
        or prompt == expected_prompt_step
        or out.get("completed") is True
    )
    return ok, out, ms


def _make_auth_headers() -> dict[str, str]:
    """Mint a service-JWT signed with the configured SECURITY__JWT_SECRET so
    the demo runner can call the API-auth-gated workflow endpoints
    (/workflow/campaign/start, /workflow/inbound, /workflow/status).
    Webhook event endpoints use HMAC signature instead — handled in _step."""

    sec = settings.security
    if not sec.jwt_secret:
        return {}
    claims: dict[str, Any] = {
        "sub": "staging-demo-runner",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    if sec.jwt_issuer:
        claims["iss"] = sec.jwt_issuer
    if sec.jwt_audience:
        claims["aud"] = sec.jwt_audience
    token = pyjwt.encode(claims, sec.jwt_secret, algorithm=sec.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def run(base_url: str, identity: str, channel: str) -> int:
    client = httpx.Client(base_url=base_url, headers=_make_auth_headers())
    # Per-invocation event-id nonce so back-to-back demo runs don't get
    # deduped by the webhook chokepoint (which retains seen ids for 24h).
    run_nonce = secrets.token_hex(6)  # noqa: F841 — referenced via f-strings below
    print(f"=== driving demo flow: identity={identity} channel={channel} nonce={run_nonce} ===\n")

    started: list[dict[str, Any]] = []

    def report(name: str, ok: bool, out: dict[str, Any], ms: float) -> None:
        prompt = (out.get("prompt") or {}).get("step") if isinstance(out, dict) else None
        completed = out.get("completed", False)
        outcome = out.get("outcome")
        line = {
            "step": name,
            "ok": ok,
            "ms": round(ms, 1),
            "prompt_step": prompt,
            "completed": completed,
            "outcome": outcome,
        }
        if not ok:
            line["error"] = out.get("error")
        print(json.dumps(line))
        started.append(line)

    inbound = {"channel": channel, "identity": identity}

    ok, out, ms = _step(
        client, "1.start", "/workflow/campaign/start", inbound,
        expected_prompt_step="campaign",
    )
    report("1.start", ok, out, ms)
    if not ok:
        return 1

    ok, out, ms = _step(
        client, "2.YES", "/workflow/inbound", {**inbound, "text": "YES"},
        expected_prompt_step="consent_cr",  # existing-user fast-path
    )
    report("2.YES", ok, out, ms)

    ok, out, ms = _step(
        client, "3.cr_upload", "/workflow/inbound",
        {**inbound, "attachments": [{"filename": "CR.pdf", "content_base64": "JVBERi0xLjQK"}]},
        expected_prompt_step="eligibility",
    )
    report("3.cr_upload", ok, out, ms)

    eligibility_form = {
        "is_qatar_based": True,
        "business_age": 5,
        "cr_validity": "VALID",
        "company_type": "LLC",
        "sector": "trade",
        "turnover": 1_000_000,
        "employees": 10,
    }
    ok, out, ms = _step(
        client, "4.eligibility_form", "/workflow/inbound",
        {**inbound, "data": eligibility_form},
        expected_prompt_step="financials",
    )
    report("4.eligibility_form", ok, out, ms)

    ok, out, ms = _step(
        client, "5.financials", "/workflow/inbound",
        {**inbound, "attachments": [{"filename": "Audited.pdf", "content_base64": "JVBERi0xLjQK"}]},
        expected_prompt_step="buyers",
    )
    report("5.financials", ok, out, ms)

    buyer_data = {
        "name": "ACME Trading LLC",
        "buyer_type": "DOMESTIC",
        "buyer_sector": "trade",
    }
    ok, out, ms = _step(
        client, "6.buyers", "/workflow/inbound",
        {**inbound, "data": buyer_data},
        expected_prompt_step="shareholders",
    )
    report("6.buyers", ok, out, ms)

    shareholders_data = {
        "shareholders": [
            {
                "name": "Developer Shareholder",
                "phoneNumber": "+97455500001",
                "firstName": "Developer",
                "lastName": "Shareholder",
                "email": "developer.shareholder@example.com",
            }
        ]
    }
    ok, out, ms = _step(
        client, "7.shareholders", "/workflow/inbound",
        {**inbound, "data": shareholders_data},
        expected_prompt_step="documents",
    )
    report("7.shareholders", ok, out, ms)

    ok, out, ms = _step(
        client, "8.documents", "/workflow/inbound",
        {
            **inbound,
            "attachments": [
                {"filename": "Trade_License.pdf", "content_base64": "JVBERi0xLjQK"},
                {"filename": "Tax_Card.pdf", "content_base64": "JVBERi0xLjQK"},
                {"filename": "Bank_Statement.pdf", "content_base64": "JVBERi0xLjQK"},
            ],
        },
        expected_prompt_step="journey_wait",
    )
    report("8.documents", ok, out, ms)

    # 9. Fire a status_update so the journey-status poll advances the run.
    ok, out, ms = _step(
        client, "9.status_update→payment",
        "/workflow/madad/events/eligibility.updated",
        {**inbound, "event_id": f"demo-evt-{run_nonce}-1", "payload": {}},
    )
    report("9.status_update→payment", ok, out, ms)

    # 10. Mark payment paid → lender_wait.
    ok, out, ms = _step(
        client, "10.payment.completed",
        "/workflow/madad/events/payment.completed",
        {**inbound, "event_id": f"demo-evt-{run_nonce}-2", "payload": {"paid": True}},
    )
    report("10.payment.completed", ok, out, ms)

    # 11. Final status_update → offer_handoff terminal.
    ok, out, ms = _step(
        client, "11.final→handoff",
        "/workflow/madad/events/offers.available",
        {**inbound, "event_id": f"demo-evt-{run_nonce}-3", "payload": {}},
    )
    report("11.final→handoff", ok, out, ms)

    print("\n=== summary ===")
    passed = sum(1 for s in started if s["ok"])
    total = len(started)
    final_outcome = started[-1].get("outcome") if started else None
    print(json.dumps({
        "total_steps": total,
        "passed": passed,
        "failed": total - passed,
        "final_outcome": final_outcome,
        "completed": started[-1].get("completed") if started else False,
    }))
    return 0 if passed == total else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8001",
        help="Workflow service base URL (default: http://localhost:8001).",
    )
    parser.add_argument(
        "--identity",
        required=True,
        help="Channel identity for the demo run — existing-user email or phone.",
    )
    parser.add_argument(
        "--channel",
        default="email",
        choices=["whatsapp", "email"],
        help="Channel for the demo run (default: email).",
    )
    args = parser.parse_args(argv)
    return run(args.base_url, args.identity, args.channel)


if __name__ == "__main__":
    sys.exit(main())
