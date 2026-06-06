"""End-to-end Phase 1.a demo driver with REAL DOCUMENTS.

Identical wire protocol to ``staging_demo_runner`` (JWT-auth on
/workflow/inbound + /workflow/campaign/start, HMAC on webhook events)
but reads real PDF/image files from disk and base64-encodes them on
the fly so the demo's KYC uploads, CR upload, and audited-financial
upload all land on Madad's backend as actual stored documents.

Designed to be run by Ishan (or anyone with the agent base-URL +
SECURITY__JWT_SECRET + SECURITY__WEBHOOK_SECRET) from any laptop —
only stdlib + httpx + pyjwt required.

Usage (typical demo run)::

    python scripts/phase1a_demo.py \\
        --base-url http://34.18.50.97:8001 \\
        --identity tech.external1@madadfintech.com \\
        --channel email \\
        --jwt-secret  "$AGENT_JWT_SECRET"     \\
        --webhook-secret "$AGENT_WEBHOOK_SECRET" \\
        --cr-file        ./real_docs/cr.pdf            \\
        --financials-file ./real_docs/audited_report.pdf \\
        --kyc-dir        ./real_docs/kyc/

    # --kyc-dir is a directory containing the admin-requested docs;
    # one file per type. Filenames are matched against
    # {trade_license, tax_card, bank_statement, vat_certificate,
    #  establishment_card} — case-insensitive substring.

What it does, per step:

    1.  POST /workflow/campaign/start          → run starts at campaign_await
    2.  POST /workflow/inbound text="YES"      → existing-user fast-path → consent_cr
    3.  POST /workflow/inbound CR.pdf attached → KYC CR upload → eligibility await
    4.  POST /workflow/inbound data={7 fields} → KYC_UPDATE_ELIGIBILITY → financials
    5.  POST /workflow/inbound Audited.pdf     → KYC financial-report upload → buyers
    6.  POST /workflow/inbound data={buyer}    → KYC_ADD_BUYER → shareholders
    7.  POST /workflow/inbound data={shares}   → KYC_ADD_SHAREHOLDERS → documents
    8.  POST /workflow/inbound kyc attachments → KYC_UPLOAD_DOCUMENT_BASE64 per file
    9.  POST /webhook event eligibility.updated → workflow advances to payment chain
    10. POST /webhook event payment.completed   → workflow advances to lender_wait
    11. POST /webhook event offers.available    → TERMINAL: offer_handoff

Exit code is 0 if every step reached its expected next-prompt, 1 otherwise.

NB: this driver is intentionally NOT installed as a package — copy this
file, set env vars, run it. No build step.
"""

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

# -- file → attachment helper -------------------------------------------------


def _attachment_from_path(path: Path) -> dict[str, Any]:
    """Read a real file and produce the attachment dict the workflow's
    ``/workflow/inbound`` expects: ``{filename, content_base64}``.

    The workflow's upload nodes infer ``mime_type`` and ``document_type``
    from the filename, so use descriptive filenames (e.g.
    ``trade_license.pdf``, ``tax_card.pdf``)."""

    data = path.read_bytes()
    return {
        "filename": path.name,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def _gather_kyc_files(kyc_dir: Path) -> list[dict[str, Any]]:
    """Walk the --kyc-dir directory and return one attachment per matched
    document type. Filename substring → workflow document_type mapping:

        trade_license / TL          → trade_license
        tax_card / tax              → tax_card
        bank_statement / bank       → bank_statement
        vat                         → vat_certificate
        establishment / est_card    → establishment_card

    Any file not matching a known doc-type is uploaded as-is and the
    workflow's filename-keyword inference does the rest.
    """

    if not kyc_dir.is_dir():
        return []
    attachments: list[dict[str, Any]] = []
    for path in sorted(kyc_dir.iterdir()):
        if not path.is_file():
            continue
        attachments.append(_attachment_from_path(path))
    return attachments


# -- auth helpers (mint JWT + HMAC) -------------------------------------------


def _make_auth_headers(jwt_secret: str | None) -> dict[str, str]:
    if not jwt_secret:
        return {}
    claims: dict[str, Any] = {
        "sub": "phase1a-demo-runner",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    token = pyjwt.encode(claims, jwt_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _hmac_sign(raw: bytes, webhook_secret: str | None) -> str | None:
    if not webhook_secret:
        return None
    return hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()


# -- HTTP wrapper -------------------------------------------------------------


def _post(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    *,
    webhook_secret: str | None,
) -> tuple[int, dict[str, Any]]:
    """Returns (status, parsed json or {error: ...})."""

    raw = json.dumps(body).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    sig = _hmac_sign(raw, webhook_secret)
    if sig and "/workflow/madad/events/" in path:
        headers["X-Madad-Signature"] = sig
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
) -> bool:
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
        f"{icon} {label:30s} {ms:7.1f}ms  status={status}  "
        f"prompt={prompt_step!r:20s}  completed={completed}  outcome={outcome}"
    )
    if not ok:
        print(f"    └─ response: {json.dumps(data)[:300]}")
    return ok


# -- main demo flow -----------------------------------------------------------


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
    # Schema (Ishan, 2026-06-06): required name + phoneNumber;
    # optional firstName/lastName/middleName/email/address.
    "name": "Aisha Karim",
    "phoneNumber": "+97455500001",
    "firstName": "Aisha",
    "lastName": "Karim",
    "email": "aisha.karim@example.com",
}


def run(
    *,
    base_url: str,
    identity: str,
    channel: str,
    jwt_secret: str | None,
    webhook_secret: str | None,
    cr_file: Path,
    financials_file: Path,
    kyc_dir: Path,
    eligibility: dict[str, Any],
    buyer: dict[str, Any],
    shareholder: dict[str, Any],
) -> int:
    headers = _make_auth_headers(jwt_secret)
    client = httpx.Client(base_url=base_url, headers=headers)
    nonce = secrets.token_hex(6)
    inbound = {"channel": channel, "identity": identity}

    print(f"=== Phase 1.a demo: identity={identity} channel={channel} nonce={nonce} ===")
    print(f"    CR:         {cr_file}")
    print(f"    Financials: {financials_file}")
    print(f"    KYC docs:   {kyc_dir}")
    print()

    all_ok = True

    all_ok &= _step(
        client, "1.  campaign start", "/workflow/campaign/start", inbound,
        webhook_secret=webhook_secret, expected_prompt_step="campaign",
    )

    all_ok &= _step(
        client, "2.  YES → check_contact", "/workflow/inbound",
        {**inbound, "text": "YES"},
        webhook_secret=webhook_secret, expected_prompt_step="consent_cr",
    )

    cr_att = _attachment_from_path(cr_file)
    all_ok &= _step(
        client, "3.  CR upload (real PDF)", "/workflow/inbound",
        {**inbound, "attachments": [cr_att]},
        webhook_secret=webhook_secret, expected_prompt_step="eligibility",
    )

    all_ok &= _step(
        client, "4.  eligibility form", "/workflow/inbound",
        {**inbound, "data": eligibility},
        webhook_secret=webhook_secret, expected_prompt_step="financials",
    )

    fin_att = _attachment_from_path(financials_file)
    all_ok &= _step(
        client, "5.  financials upload (real PDF)", "/workflow/inbound",
        {**inbound, "attachments": [fin_att]},
        webhook_secret=webhook_secret, expected_prompt_step="buyers",
    )

    all_ok &= _step(
        client, "6.  buyer", "/workflow/inbound",
        {**inbound, "data": buyer},
        webhook_secret=webhook_secret, expected_prompt_step="shareholders",
    )

    all_ok &= _step(
        client, "7.  shareholders", "/workflow/inbound",
        {**inbound, "data": {"shareholders": [shareholder]}},
        webhook_secret=webhook_secret, expected_prompt_step="documents",
    )

    kyc_atts = _gather_kyc_files(kyc_dir)
    if not kyc_atts:
        print(
            f"⚠️  --kyc-dir {kyc_dir} is empty; the documents step may loop."
        )
    all_ok &= _step(
        client, f"8.  KYC docs ({len(kyc_atts)} files)", "/workflow/inbound",
        {**inbound, "attachments": kyc_atts},
        webhook_secret=webhook_secret, expected_prompt_step="journey_wait",
    )

    all_ok &= _step(
        client, "9.  webhook → eligibility.updated",
        "/workflow/madad/events/eligibility.updated",
        {**inbound, "event_id": f"demo-evt-{nonce}-1", "payload": {}},
        webhook_secret=webhook_secret, expected_prompt_step="payment",
    )

    all_ok &= _step(
        client, "10. webhook → payment.completed",
        "/workflow/madad/events/payment.completed",
        {**inbound, "event_id": f"demo-evt-{nonce}-2", "payload": {"paid": True}},
        webhook_secret=webhook_secret, expected_prompt_step="lender_wait",
    )

    all_ok &= _step(
        client, "11. webhook → offers.available (TERMINAL)",
        "/workflow/madad/events/offers.available",
        {**inbound, "event_id": f"demo-evt-{nonce}-3", "payload": {}},
        webhook_secret=webhook_secret, expected_terminal=True,
    )

    print()
    print("=" * 70)
    if all_ok:
        print("🎉 PHASE 1.a DEMO PASSED — all 11 steps green, terminal=offer_handoff")
        return 0
    print("⚠️  Some steps failed — see ❌ rows above")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://34.18.50.97:8001")
    parser.add_argument("--identity", required=True)
    parser.add_argument("--channel", default="email", choices=["whatsapp", "email"])
    parser.add_argument("--jwt-secret", default=None)
    parser.add_argument("--webhook-secret", default=None)
    parser.add_argument(
        "--cr-file", type=Path, required=True,
        help="Path to a real PDF of the SME's Commercial Registration.",
    )
    parser.add_argument(
        "--financials-file", type=Path, required=True,
        help="Path to a real PDF of the SME's audited financial report.",
    )
    parser.add_argument(
        "--kyc-dir", type=Path, required=True,
        help=(
            "Path to a directory of admin-requested KYC documents. "
            "One file per document type; filename keywords ('trade_license', "
            "'tax_card', 'bank_statement', etc.) drive the backend label."
        ),
    )
    parser.add_argument(
        "--eligibility", type=Path, default=None,
        help="Optional JSON file overriding the 7 eligibility fields.",
    )
    parser.add_argument(
        "--buyer", type=Path, default=None,
        help="Optional JSON file overriding the buyer record.",
    )
    parser.add_argument(
        "--shareholder", type=Path, default=None,
        help="Optional JSON file overriding the shareholder record.",
    )
    args = parser.parse_args(argv)

    eligibility = (
        json.loads(args.eligibility.read_text()) if args.eligibility else DEFAULT_ELIGIBILITY
    )
    buyer = json.loads(args.buyer.read_text()) if args.buyer else DEFAULT_BUYER
    shareholder = (
        json.loads(args.shareholder.read_text()) if args.shareholder else DEFAULT_SHAREHOLDER
    )

    return run(
        base_url=args.base_url,
        identity=args.identity,
        channel=args.channel,
        jwt_secret=args.jwt_secret,
        webhook_secret=args.webhook_secret,
        cr_file=args.cr_file,
        financials_file=args.financials_file,
        kyc_dir=args.kyc_dir,
        eligibility=eligibility,
        buyer=buyer,
        shareholder=shareholder,
    )


if __name__ == "__main__":
    sys.exit(main())
