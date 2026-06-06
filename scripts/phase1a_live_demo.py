"""End-to-end Phase 1.a LIVE demo — sends real WhatsApp messages and a real email
to a target user while driving the full backend flow.

Difference from ``phase1a_demo.py``: that script drives the workflow against
the staging agent and reports pass/fail in the terminal. This one also opens
a side-channel to the Madad MCP cluster and:

  * Sends a real WhatsApp narration to ``--your-phone`` at each of the 11 steps
    via ``madad_external_send_whatsapp_text`` (works for arbitrary content —
    user gets ~12 messages on their phone in real time).
  * After the workflow creates the monetization payment, also calls
    ``madad_payments_send_monetization_payment_link`` with the user's actual
    ``--your-email`` so the Madad-branded payment email arrives in *their*
    inbox (not the backend test SME's).

The agent workflow itself runs against the existing test SME
(``tech.external1@madadfintech.com``) so all backend operations — KYC uploads,
shareholders, buyers, payment record, journey-status updates — produce real
records on Madad's UAT cluster.

Usage::

    python scripts/phase1a_live_demo.py \\
        --base-url        http://34.18.50.97:8001 \\
        --jwt-secret      "$AGENT_JWT_SECRET" \\
        --webhook-secret  "$AGENT_WEBHOOK_SECRET" \\
        --mcp-endpoint    https://madad-mcp-cluster-626656664233.me-central1.run.app/mcp \\
        --mcp-token       "$MCP_AUTH_TOKEN" \\
        --your-phone      +919497191690 \\
        --your-email      jathish.namboothiri@gmail.com \\
        --cr-file         real_docs/cr.pdf \\
        --financials-file real_docs/audited.pdf \\
        --kyc-dir         real_docs/kyc/

Only stdlib + httpx + pyjwt required."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_IDENTITY = "tech.external1@madadfintech.com"
BACKEND_CHANNEL = "email"


def _attachment_from_path(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "filename": path.name,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def _gather_kyc_files(kyc_dir: Path) -> list[dict[str, Any]]:
    if not kyc_dir.is_dir():
        return []
    return [
        _attachment_from_path(p)
        for p in sorted(kyc_dir.iterdir())
        if p.is_file()
    ]


def _make_auth_headers(jwt_secret: str) -> dict[str, str]:
    claims = {
        "sub": "phase1a-live-demo",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    token = pyjwt.encode(claims, jwt_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _hmac_sign(raw: bytes, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()


class McpClient:
    """Minimal Streamable-HTTP MCP client — POST one JSON-RPC envelope, parse the
    SSE-or-JSON reply. The Madad cluster returns ``text/event-stream`` with a
    single ``data: {...}`` line, so we parse it permissively."""

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self._http = httpx.Client(timeout=60.0)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        r = self._http.post(
            self.endpoint,
            json=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"MCP HTTP {r.status_code}: {r.text[:300]}")
        text = r.text
        if "data: " in text:
            for line in text.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: ") :])
                    break
            else:
                raise RuntimeError(f"no data line in SSE: {text[:200]}")
        else:
            payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"MCP error: {payload['error']}")
        result = payload.get("result", {})
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                parsed = json.loads(item["text"])
                # Unwrap Madad's universal {status_code, body} envelope.
                if isinstance(parsed, dict) and "body" in parsed:
                    body = parsed["body"]
                    if isinstance(body, list):
                        return {"items": body}
                    if isinstance(body, dict):
                        return body
                if isinstance(parsed, list):
                    return {"items": parsed}
                if isinstance(parsed, dict):
                    return parsed
        assert isinstance(result, dict)
        return result


def _wa_narrate(mcp: McpClient | None, phone: str | None, body: str) -> None:
    if not (mcp and phone):
        return
    try:
        mcp.call("madad_external_send_whatsapp_text", {"to": phone, "body": body})
    except Exception as e:
        print(f"    ⚠ WhatsApp narration failed: {repr(e)[:120]}")


def _post(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    *,
    webhook_secret: str | None,
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if webhook_secret and "/workflow/madad/events/" in path:
        headers["X-Madad-Signature"] = _hmac_sign(raw, webhook_secret)
        r = client.post(path, content=raw, headers=headers, timeout=60.0)
    else:
        r = client.post(path, json=body, timeout=60.0)
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"_raw": r.text[:200]}
    return r.status_code, data


def _step(
    client: httpx.Client,
    label: str,
    path: str,
    body: dict[str, Any],
    *,
    webhook_secret: str | None,
    expected_prompt_step: str | None = None,
    expected_terminal: bool = False,
    mcp: McpClient | None = None,
    phone: str | None = None,
    wa_narration: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    start = time.perf_counter()
    status, data = _post(client, path, body, webhook_secret=webhook_secret)
    ms = (time.perf_counter() - start) * 1000

    prompt_step = (data.get("prompt") or {}).get("step") if isinstance(data, dict) else None
    completed = bool(data.get("completed")) if isinstance(data, dict) else False
    outcome = data.get("outcome") if isinstance(data, dict) else None

    ok = status == 200
    if ok and expected_prompt_step is not None:
        ok = prompt_step == expected_prompt_step
    if ok and expected_terminal:
        ok = completed is True

    icon = "✅" if ok else "❌"
    print(
        f"{icon} {label:35s} {ms:7.1f}ms  prompt={prompt_step!r:20s}  "
        f"completed={completed}  outcome={outcome}"
    )
    if not ok:
        print(f"    └─ response: {json.dumps(data)[:300]}")

    if ok and wa_narration:
        _wa_narrate(mcp, phone, wa_narration)
    return ok, data


DEFAULT_ELIGIBILITY: dict[str, Any] = {
    "is_qatar_based": True,
    "business_age": "UNDER_2_YEARS",
    "cr_validity": "UNDER_1_MONTH",
    "company_type": "LLC",
    "sector": "trade",
    "turnover": "1000000",
    "employees": "10",
}

DEFAULT_BUYER: dict[str, Any] = {
    "name": "ACME Trading LLC",
    "buyer_type": "DOMESTIC",
    "buyer_sector": "trade",
}

DEFAULT_SHAREHOLDER: dict[str, Any] = {
    "name": "Aisha Karim",
    "phoneNumber": "+97455500001",
    "firstName": "Aisha",
    "lastName": "Karim",
    "email": "aisha.karim@example.com",
}

NARRATIONS = {
    "start": (
        "👋 [MADAD demo] Starting Phase 1.a SME onboarding journey now. "
        "You'll receive ~10 messages over the next minute as the agent works."
    ),
    "1_campaign": (
        "1️⃣  Campaign opt-in sent. Reply YES to start your invoice-financing "
        "journey."
    ),
    "2_yes": (
        "2️⃣  You're identified as an existing MADAD user. Please upload your "
        "Commercial Registration (CR) document."
    ),
    "3_cr": (
        "3️⃣  CR uploaded ✓. Now please answer 7 quick eligibility questions "
        "about your business."
    ),
    "4_elig": (
        "4️⃣  Eligibility looks good ✓. Please upload your latest audited "
        "financial report."
    ),
    "5_fin": (
        "5️⃣  Financials uploaded ✓. Tell me about a buyer (counterparty) "
        "you'd like to factor invoices for."
    ),
    "6_buyer": (
        "6️⃣  Buyer 'ACME Trading LLC' added ✓. Now please share at least "
        "one shareholder of your business."
    ),
    "7_share": (
        "7️⃣  Shareholders added ✓. Final step: upload Trade License, "
        "Tax Card and Bank Statement."
    ),
    "8_kyc": (
        "8️⃣  KYC documents uploaded ✓. Submitting your application for "
        "review now."
    ),
    "9_elig_webhook": (
        "9️⃣  🎉 You're PRE-QUALIFIED for invoice financing. A QAR 6,000 "
        "onboarding payment link has been emailed to you — check your inbox."
    ),
    "10_paid": (
        "🔟 Payment received ✓. Your application is now under final "
        "lender review."
    ),
    "11_offers": (
        "🏆 Congrats! Your lender offers are ready. A MADAD analyst will "
        "reach out shortly to walk you through the terms. Demo complete."
    ),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://34.18.50.97:8001")
    p.add_argument("--jwt-secret", required=True)
    p.add_argument("--webhook-secret", required=True)
    p.add_argument("--mcp-endpoint", required=True)
    p.add_argument("--mcp-token", required=True)
    p.add_argument("--your-phone", required=True, help="E.164 phone for WhatsApp narration")
    p.add_argument("--your-email", required=True, help="Email for Madad payment-link delivery")
    p.add_argument("--cr-file", type=Path, required=True)
    p.add_argument("--financials-file", type=Path, required=True)
    p.add_argument("--kyc-dir", type=Path, required=True)
    args = p.parse_args()

    if not args.cr_file.is_file():
        sys.exit(f"--cr-file not found: {args.cr_file}")
    if not args.financials_file.is_file():
        sys.exit(f"--financials-file not found: {args.financials_file}")

    mcp = McpClient(args.mcp_endpoint, args.mcp_token)
    nonce = secrets.token_hex(6)

    print(f"=== Phase 1.a LIVE demo (nonce={nonce}) ===")
    print(f"  backend identity: {BACKEND_IDENTITY}")
    print(f"  your WhatsApp:    {args.your_phone}")
    print(f"  your email:       {args.your_email}")
    print()

    _wa_narrate(mcp, args.your_phone, NARRATIONS["start"])

    headers = _make_auth_headers(args.jwt_secret)
    client = httpx.Client(base_url=args.base_url, headers=headers)
    all_ok = True
    captured_payment_id: str | None = None

    ok, _ = _step(
        client, "1.  campaign start", "/workflow/campaign/start",
        {"channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY},
        webhook_secret=None, expected_prompt_step="campaign",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["1_campaign"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "2.  YES → check_contact", "/workflow/inbound",
        {"channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY, "text": "YES"},
        webhook_secret=None, expected_prompt_step="consent_cr",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["2_yes"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "3.  CR upload (real PDF)", "/workflow/inbound",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "attachments": [_attachment_from_path(args.cr_file)],
        },
        webhook_secret=None, expected_prompt_step="eligibility",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["3_cr"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "4.  eligibility form", "/workflow/inbound",
        {"channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY, "data": DEFAULT_ELIGIBILITY},
        webhook_secret=None, expected_prompt_step="financials",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["4_elig"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "5.  financials upload (real PDF)", "/workflow/inbound",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "attachments": [_attachment_from_path(args.financials_file)],
        },
        webhook_secret=None, expected_prompt_step="buyers",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["5_fin"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "6.  buyer", "/workflow/inbound",
        {"channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY, "data": DEFAULT_BUYER},
        webhook_secret=None, expected_prompt_step="shareholders",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["6_buyer"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "7.  shareholders", "/workflow/inbound",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "data": {"shareholders": [DEFAULT_SHAREHOLDER]},
        },
        webhook_secret=None, expected_prompt_step="documents",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["7_share"],
    )
    all_ok &= ok

    kyc_atts = _gather_kyc_files(args.kyc_dir)
    if not kyc_atts:
        print(f"⚠️  --kyc-dir {args.kyc_dir} is empty; documents step may loop.")
    ok, _ = _step(
        client, "8.  KYC docs", "/workflow/inbound",
        {"channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY, "attachments": kyc_atts},
        webhook_secret=None, expected_prompt_step="journey_wait",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["8_kyc"],
    )
    all_ok &= ok

    ok, data = _step(
        client, "9.  webhook eligibility.updated", "/workflow/madad/events/eligibility.updated",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "event_id": f"live-elig-{nonce}",
            "payload": {"journey_status": "PRE_QUALIFIED"},
        },
        webhook_secret=args.webhook_secret, expected_prompt_step="payment",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["9_elig_webhook"],
    )
    all_ok &= ok
    del data, captured_payment_id

    # === SIDE CHANNEL: mint a fresh payment via MCP and send the link to the
    # USER's actual email address (the workflow's send_link goes to the
    # backend test SME's mailbox, not the demo viewer's). ===
    print(f"    → minting side-channel payment + sending link to {args.your_email} via MCP …")
    try:
        session = mcp.call(
            "madad_mcp_create_channel_session",
            {"channel": "EMAIL", "identifier": BACKEND_IDENTITY},
        )
        token = session["accessToken"]
        bd = mcp.call("madad_kyc_get_business_details", {"access_token": token})
        bd_id = (bd.get("businessDetails") or {}).get("id") or bd.get("id")
        products = mcp.call(
            "madad_payments_list_monetization_products", {"access_token": token}
        )
        prod_list = products.get("items") or products.get("products") or []
        onb = next(p for p in prod_list if p.get("code") == "onboarding")
        cp = mcp.call(
            "madad_payments_create_monetization_payment",
            {
                "access_token": token,
                "idempotency_key": f"live-demo-create-{nonce}",
                "business_details_id": bd_id,
                "product_id": onb["id"],
                "payable_amount": 6000,
            },
        )
        new_payment_id = cp.get("id") or cp.get("payment_id")
        mcp.call(
            "madad_payments_send_monetization_payment_link",
            {
                "access_token": token,
                "idempotency_key": f"live-demo-user-email-{nonce}",
                "payment_id": new_payment_id,
                "recipient_email": args.your_email,
            },
        )
        print(f"    ✅ Madad-branded payment email queued to {args.your_email}")
    except Exception as e:
        print(f"    ⚠ side-channel email failed: {repr(e)[:200]}")

    ok, _ = _step(
        client, "10. webhook payment.completed", "/workflow/madad/events/payment.completed",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "event_id": f"live-paid-{nonce}",
            "payload": {"paid": True, "amount": 6000, "currency": "QAR"},
        },
        webhook_secret=args.webhook_secret, expected_prompt_step="lender_wait",
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["10_paid"],
    )
    all_ok &= ok

    ok, _ = _step(
        client, "11. webhook offers.available", "/workflow/madad/events/offers.available",
        {
            "channel": BACKEND_CHANNEL, "identity": BACKEND_IDENTITY,
            "event_id": f"live-offers-{nonce}",
            "payload": {"journey_status": "ACCEPTED"},
        },
        webhook_secret=args.webhook_secret, expected_terminal=True,
        mcp=mcp, phone=args.your_phone,
        wa_narration=NARRATIONS["11_offers"],
    )
    all_ok &= ok

    print()
    print("=" * 78)
    if all_ok:
        print("🎉 PHASE 1.a LIVE DEMO PASSED — all 11 steps green, terminal=offer_handoff")
        print()
        print(f"   Check your WhatsApp ({args.your_phone}) for ~12 messages.")
        print(f"   Check your email ({args.your_email}) for the Madad payment link.")
        return 0
    print("❌ DEMO FAILED — see ❌ steps above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
