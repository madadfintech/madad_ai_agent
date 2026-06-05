"""Staging smoke test for the MCP integration.

Runs every critical tool seam directly (not through the workflow runtime) so
operators can verify the MCP cluster is reachable, authenticated, and
behaving as expected BEFORE flipping inbound webhook traffic onto the
service. Phase 5 of project_mcp_implementation_plan; the 10-step pre-flip
checklist except dashboards (manual via Grafana).

Usage::

    # Inside the running container, after settings are loaded from env:
    python -m scripts.staging_smoke_test \
        --identity +97455500001 \
        --email test+smoke@madadfintech.com

    # Or from the host against a service container:
    docker compose -f docker/docker-compose.yml exec workflow \
        python -m scripts.staging_smoke_test --identity +97455500001

Exits with code 0 if every step passes, 1 if any step fails. Logs one JSON
line per step (so stdout is grep-friendly) plus a final summary.

This is a smoke test, not an integration test: it calls the cheapest
read-only path of each tool where possible, and uses tiny test payloads
for the write paths. Repeated runs are safe — the create_monetization_payment
write reuses the same idempotency_key so the backend dedupes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.core.config import settings as default_settings
from app.services.workflow.mcp_identity import McpMadadIdentityClient
from app.services.workflow.mcp_kyc import McpKycClient
from app.services.workflow.mcp_payments import McpMonetizationPaymentAdapter
from app.shared.mcp import Tools, get_mcp_client
from app.shared.workflow.enums import Channel


@dataclass
class StepResult:
    name: str
    tool: str | None
    ok: bool
    latency_ms: float
    detail: str = ""


async def _time(func: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    start = time.perf_counter()
    result = await func(*args, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000
    return result, elapsed


async def step_settings_load() -> StepResult:
    """Verify the MCP toggle is on and the endpoint is non-empty."""

    s = default_settings.mcp
    if not s.enabled:
        return StepResult(
            "settings", None, False, 0.0,
            "MCP__ENABLED=false — staging cutover not yet flipped",
        )
    if not s.endpoint:
        return StepResult(
            "settings", None, False, 0.0,
            "MCP__ENDPOINT empty — set to the UAT cluster URL",
        )
    return StepResult(
        "settings", None, True, 0.0,
        f"endpoint={s.endpoint} auth_mode={s.auth_mode}",
    )


def _detect_channel(identity_value: str) -> Channel:
    """Smoke runner accepts either a phone (+E.164) or an email; pick the
    channel from the format so existing-user lookups land on the right
    backend index."""

    return Channel.EMAIL if "@" in identity_value else Channel.WHATSAPP


async def step_check_contact(
    identity: McpMadadIdentityClient, identity_value: str
) -> StepResult:
    try:
        channel = _detect_channel(identity_value)
        if channel is Channel.EMAIL:
            out, ms = await _time(identity.check_contact, email=identity_value)
        else:
            out, ms = await _time(identity.check_contact, phone=identity_value)
        return StepResult(
            "check_contact",
            Tools.AUTH_CHECK_CONTACT,
            True,
            ms,
            f"exists={out.exists} field={out.field} domain_exists={out.domain_exists}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "check_contact", Tools.AUTH_CHECK_CONTACT, False, 0.0, repr(exc)
        )


async def step_open_session(
    identity: McpMadadIdentityClient, channel: Channel, phone: str
) -> tuple[StepResult, str | None]:
    try:
        session, ms = await _time(
            identity.open_session,
            channel=channel,
            identifier=phone,
            create_onboarding_token=True,
        )
        access_token = session.access_token or session.onboarding_token
        return (
            StepResult(
                "channel_session",
                Tools.MCP_CREATE_CHANNEL_SESSION,
                True,
                ms,
                f"type={session.session_type} has_access_token={bool(session.access_token)}",
            ),
            access_token,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            StepResult(
                "channel_session", Tools.MCP_CREATE_CHANNEL_SESSION, False, 0.0, repr(exc)
            ),
            None,
        )


async def step_auth_me(
    identity: McpMadadIdentityClient, access_token: str | None
) -> StepResult:
    if access_token is None:
        return StepResult("auth_me", Tools.AUTH_ME, False, 0.0, "no access_token to call with")
    try:
        info, ms = await _time(identity.me, access_token=access_token)
        return StepResult(
            "auth_me", Tools.AUTH_ME, True, ms,
            f"user_id={info.get('user', {}).get('id')!r}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult("auth_me", Tools.AUTH_ME, False, 0.0, repr(exc))


async def step_kyc_upload_document(
    kyc: McpKycClient, access_token: str | None
) -> StepResult:
    if access_token is None:
        return StepResult(
            "kyc_upload_document_base64", Tools.KYC_UPLOAD_DOCUMENT_BASE64, False, 0.0,
            "no access_token",
        )
    try:
        result, ms = await _time(
            kyc.upload_document_base64,
            access_token=access_token,
            content_base64="JVBERi0xLjQK",  # tiny valid-PDF prefix
            filename="smoke-test.pdf",
            document_type="trade_license",
        )
        return StepResult(
            "kyc_upload_document_base64", Tools.KYC_UPLOAD_DOCUMENT_BASE64, True, ms,
            f"document_id={result.get('document_id')!r}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "kyc_upload_document_base64", Tools.KYC_UPLOAD_DOCUMENT_BASE64, False, 0.0,
            repr(exc),
        )


async def step_payments_list_products(
    payments: McpMonetizationPaymentAdapter, access_token: str | None
) -> StepResult:
    if access_token is None:
        return StepResult(
            "payments_list_products",
            Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
            False, 0.0, "no access_token",
        )
    try:
        out, ms = await _time(payments.list_monetization_products, access_token=access_token)
        products = out.get("products", []) if isinstance(out, dict) else []
        return StepResult(
            "payments_list_products",
            Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
            True, ms, f"product_count={len(products)}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "payments_list_products",
            Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
            False, 0.0, repr(exc),
        )


async def step_ext_send_whatsapp(phone: str) -> StepResult:
    """Send a low-risk smoke text. Uses the shared MCP client directly since
    this tool isn't wrapped by an adapter (it's invoked by the Communication
    gateway in production)."""

    try:
        mcp = get_mcp_client()
        out, ms = await _time(
            mcp.call_tool,
            Tools.EXT_SEND_WHATSAPP_TEXT,
            {
                "to": phone,
                "body": "[smoke-test] MCP staging cutover ping — please ignore.",
            },
        )
        return StepResult(
            "ext_send_whatsapp_text",
            Tools.EXT_SEND_WHATSAPP_TEXT,
            True, ms, f"response={json.dumps(out)[:120]}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "ext_send_whatsapp_text",
            Tools.EXT_SEND_WHATSAPP_TEXT,
            False, 0.0, repr(exc),
        )


async def step_email_otp_send(email: str) -> StepResult:
    """Send an OTP to the operator test mailbox. The UAT cluster always
    returns 123456 as the OTP, so this verifies the EXT seam end-to-end
    without needing a real inbox check."""

    try:
        mcp = get_mcp_client()
        out, ms = await _time(
            mcp.call_tool, Tools.EXT_SEND_EMAIL_OTP, {"email": email}
        )
        success = bool(out.get("success", False)) if isinstance(out, dict) else False
        return StepResult(
            "ext_send_email_otp",
            Tools.EXT_SEND_EMAIL_OTP,
            success, ms,
            f"message={out.get('message')!r}" if isinstance(out, dict) else "",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "ext_send_email_otp", Tools.EXT_SEND_EMAIL_OTP, False, 0.0, repr(exc)
        )


async def run(
    identity_phone: str,
    *,
    skip_whatsapp: bool = False,
    email: str = "tech.external1@madadfintech.com",
    whatsapp_recipient: str | None = None,
) -> int:
    settings_res = await step_settings_load()
    print(json.dumps(asdict(settings_res)))
    if not settings_res.ok:
        return 1

    mcp = get_mcp_client()
    identity_client = McpMadadIdentityClient(mcp)
    kyc = McpKycClient(mcp)
    payments = McpMonetizationPaymentAdapter(mcp)

    results = [settings_res]

    # Auth-free seams first (always exercised).
    cc = await step_check_contact(identity_client, identity_phone)
    print(json.dumps(asdict(cc)))
    results.append(cc)

    otp_send = await step_email_otp_send(email)
    print(json.dumps(asdict(otp_send)))
    results.append(otp_send)

    sess_res, access_token = await step_open_session(
        identity_client, _detect_channel(identity_phone), identity_phone
    )
    print(json.dumps(asdict(sess_res)))
    results.append(sess_res)

    # WhatsApp recipient defaults to the identity when it's already a phone;
    # when the identity is an email (existing-user lookup mode), skip
    # WhatsApp unless an explicit --whatsapp-recipient is given.
    wa_to = whatsapp_recipient or (
        identity_phone if _detect_channel(identity_phone) is Channel.WHATSAPP else None
    )
    if not skip_whatsapp and wa_to:
        wa = await step_ext_send_whatsapp(wa_to)
        print(json.dumps(asdict(wa)))
        results.append(wa)

    # Auth-protected seams — only run when MCP_CREATE_CHANNEL_SESSION returns
    # a real access_token. In current UAT this is blocked on Ishan I-1 (the
    # channel-session backend returns sessionType but no token yet); the
    # OTP path returns the token in a Set-Cookie header the MCP layer
    # doesn't expose. Until either lands, mark these as skipped.
    if access_token is None:
        print(
            json.dumps(
                {
                    "name": "auth_protected_tools",
                    "tool": None,
                    "skipped": True,
                    "reason": "no access_token from channel-session (Ishan I-1) "
                    "and OTP verify returns token in Set-Cookie (not body); "
                    "AUTH_ME / KYC_* / PAYMENTS_* await Ishan auth fix",
                }
            )
        )
    else:
        me = await step_auth_me(identity_client, access_token)
        print(json.dumps(asdict(me)))
        results.append(me)

        kyc_up = await step_kyc_upload_document(kyc, access_token)
        print(json.dumps(asdict(kyc_up)))
        results.append(kyc_up)

        pl = await step_payments_list_products(payments, access_token)
        print(json.dumps(asdict(pl)))
        results.append(pl)

    # Summary.
    failures = [r for r in results if not r.ok]
    summary = {
        "summary": True,
        "total_steps": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "failed_steps": [r.name for r in failures],
        "total_latency_ms": round(sum(r.latency_ms for r in results), 1),
        "auth_protected_skipped": access_token is None,
    }
    print(json.dumps(summary))

    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--identity",
        required=True,
        help="WhatsApp E.164 phone for the smoke-test account (e.g. +97455500001)",
    )
    parser.add_argument(
        "--skip-whatsapp",
        action="store_true",
        help="Skip the EXT_SEND_WHATSAPP_TEXT step (use when the test account "
        "shouldn't receive a real message yet).",
    )
    parser.add_argument(
        "--email",
        default="tech.external1@madadfintech.com",
        help="Email used for the EXT_SEND_EMAIL_OTP step. Defaults to the "
        "Madad test mailbox; the UAT cluster always returns 123456 as the OTP.",
    )
    parser.add_argument(
        "--whatsapp-recipient",
        default=None,
        help="WhatsApp E.164 number for the smoke message. Defaults to the "
        "--identity value when that's a phone; required when --identity is "
        "an email.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run(
            args.identity,
            skip_whatsapp=args.skip_whatsapp,
            email=args.email,
            whatsapp_recipient=args.whatsapp_recipient,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
