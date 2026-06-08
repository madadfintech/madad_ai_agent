# WhatsApp Onboarding — Backend + MCP Integration Contract

This describes the WhatsApp onboarding contract now provided by the **backend** and
**MCP cluster**. It replaces the placeholder-email (`wa<digits>@…`) approach. The
agent owns the conversation/memory; this doc is the resource contract to wire against.

> Backend changes: `madadfintech/backend-api` PR #141.
> MCP changes: `madadfintech/madad_ai_mcp_cluster` (on `main`).

## 1. Create the user the moment the lead says YES (no email)

Stop calling `madad_auth_complete_onboarding` with a synthetic email. Instead, when
the lead shows intent, call the channel-session tool with `create_user_if_missing`:

```
madad_mcp_create_channel_session(
    channel="WHATSAPP",
    identifier="<phone>",        # e.g. +9745xxxxxxx — the WhatsApp number IS the identity
    phone="<phone>",
    display_name="<name or None>",
    create_user_if_missing=True,
)
```

Backend creates a `SIGN_UP` user from the phone number alone — **no email, no
password** — with empty business details, the `MERCHANT` role and initial consents,
and returns an access token. Branch on `sessionType` in the response:

| `sessionType`           | Meaning | Agent action |
|-------------------------|---------|--------------|
| `new_user_created`      | Fresh lead created | Use `accessToken`; continue to consent/CR. `referenceNumber` is the account ref. |
| `existing_user`         | A WhatsApp lead is resuming | Use `accessToken`; resume from `whatsappOnboardingStep`. |
| `existing_portal_user`  | A portal account already exists for this number (`requiresPortalLogin: true`) | Tell them their application already exists and to log in at `portalUrl`. Do **not** drive onboarding. |

The CR/business-name/shareholders are filled later by CR extraction — no need to
collect `cr_number` / `legal_entity_name` before creating the user.

## 2. Upload documents through the classify pipeline (not hardcoded types)

Use the new classify-and-upload tools for **every** inbound document whose type is
not certain. They run the exact pipeline as the MSME complete-onboarding page
(classify → map to `DocumentType` → route to entity/KYC stage → upload), and the
backend then extracts (shareholders from the CR, etc.) just like a portal drop.

Single file:

```
madad_kyc_classify_and_upload_document_base64(
    file_name=..., base64=..., access_token=..., mime_type=...,
    # optional: document_param (e.g. "fy2023" for a specific audited year)
)
```

ZIP:

```
madad_kyc_classify_and_upload_zip_base64(file_name=..., base64=..., access_token=...)
# -> { uploaded_count, documents: [{file_name, document_type, classified}], errors }
```

Use the returned per-file `documents[]` checklist to build the WhatsApp
"ZIP received / received & validated / still missing X" reply. Anything the
classifier can't place is stored as `ADDITIONAL_DOCUMENT`, so a file is never lost.
This applies to **both** the pre-prequalification CR + Audited Report step and the
post-prequalification full document set.

> Requires `DOCUMENT_CLASSIFIER_URL` set on the MCP cluster (same value as the
> portal's `NEXT_PUBLIC_DOCUMENT_IDENTIFIER_URL`).

## 3. Record the conversational step (drives the pre-qualification trigger)

After each milestone, post the step so backend gates work and the 24h window stays
fresh:

```
madad_mcp_update_onboarding_progress(
    channel="WHATSAPP", identifier="<phone>",   # or user_id=...
    step=<n>, touch_inbound=True,
)
```

Backend status ladder (driven automatically by uploads, for reference):

| Event | `journeyStatus` | Suggested `step` |
|-------|-----------------|------------------|
| Lead says YES → user created | `SIGN_UP` | 1 |
| CR uploaded | `INCOMPLETE` | 2 |
| CR + Audited Report uploaded, "pre-qualification in 24h / account created" sent | `UNVERIFIED` | **3** |
| Ops verifies the two docs | `VERIFIED` | 3 |
| Credit pre-qualifies (admin) | `PRE_QUALIFIED` | → triggers webhook (below) |

**Critical:** set `step=3` once the financials are in and you've sent the
"pre-qualification within 24 hours / account created (ref #…)" message. The backend
**only** fires the pre-qualified document checklist for leads at `step >= 3`.

## 4. Pre-qualification → document checklist (already wired backend→agent)

When an admin sets the application to `PRE_QUALIFIED`, the backend emits
`prequalification.completed` to the agent webhook **only if** the user is a WhatsApp
lead with a real phone number at `step >= 3`. On receiving it, the agent resumes the
parked run and sends the "🎉 Congratulations! …share these documents" checklist, then
collects the full document set via the classify-and-upload tools in step 2.

The post-prequalification document set does **not** downgrade the journey status —
the backend no-ops status changes once a lead is past document submission.

## 5. Net change vs the old flow

- Remove `_placeholder_email_for_phone` and the synthetic-email `complete_onboarding`
  call for new WhatsApp leads → replace with `create_user_if_missing=True`.
- Replace hardcoded-type uploads (`upload_commercial_registration`,
  `upload_document_base64` with a fixed `document_type`) with the
  `madad_kyc_classify_and_upload_*` tools.
- Add `madad_mcp_update_onboarding_progress` calls at each milestone (esp. `step=3`).
- Handle the `existing_portal_user` / `requiresPortalLogin` branch.
