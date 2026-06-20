"""Central registry of MCP tool names — single source of truth for the catalog.

Mirrors the deployed catalog at
``https://madad-mcp-cluster-626656664233.me-central1.run.app/mcp`` (Ishan's
``madadfintech/madad_ai_mcp_cluster`` repo, ``main`` branch). Tool names below
are stable and validated against ``tools/list`` on the live server.

Groups:
* ``AUTH_*`` (13) — Madad portal auth flow (the agent does NOT drive this path
  itself; ``madad_mcp_create_channel_session`` is our entry. Tools listed for
  completeness / inter-op).
* ``EXT_*`` (5) — external communications routed through the Madad backend.
* ``MCP_*`` (3) — MCP-side orchestration tools (channel session bridge + webhook
  emission).
* ``KYC_*`` (28) — the bulk of the onboarding journey. Excludes
  ``madad_kyc_complete_stage`` per Ishan's Q9 guidance (backup-only).
* ``OFFERS_*`` (4) — offer reads.
* ``PAYMENTS_*`` (5) — TESS monetization payments (QAR 6,000 onboarding fee).
  Write tools require an ``idempotency_key`` parameter.
* ``INVOICES_*`` (9) — Phase 1.b invoice financing tools (deployed early; not
  used in Phase 1.a).

Total: 67 names. The live catalog contains 68 because we deliberately omit
``madad_kyc_complete_stage``.
"""

from __future__ import annotations


class Tools:
    """MCP tool-name constants grouped by capability."""

    # -- auth (Madad portal login flow) --------------------------------------
    AUTH_SEND_OTP = "madad_auth_send_otp"
    AUTH_VERIFY_OTP = "madad_auth_verify_otp"
    AUTH_CHECK_CONTACT = "madad_auth_check_contact"
    AUTH_ME = "madad_auth_me"
    AUTH_ONBOARDING_SEND_EMAIL = "madad_auth_onboarding_send_email"
    AUTH_VERIFY_ONBOARDING_EMAIL = "madad_auth_verify_onboarding_email"
    AUTH_ONBOARDING_SEND_PHONE = "madad_auth_onboarding_send_phone"
    AUTH_ONBOARDING_VERIFY_PHONE = "madad_auth_onboarding_verify_phone"
    AUTH_COMPLETE_ONBOARDING = "madad_auth_complete_onboarding"
    AUTH_COMPLETE_GOOGLE_ONBOARDING = "madad_auth_complete_google_onboarding"
    AUTH_REFRESH = "madad_auth_refresh"
    AUTH_LOGOUT = "madad_auth_logout"
    AUTH_GOOGLE_OAUTH_URL = "madad_auth_google_oauth_url"

    # -- external communications (backend-routed) ----------------------------
    EXT_SEND_SMS_OTP = "madad_external_send_sms_otp"
    EXT_SEND_EMAIL_OTP = "madad_external_send_email_otp"
    EXT_SEND_EMAIL_TEXT = "madad_external_send_email_text"
    EXT_VERIFY_OTP = "madad_external_verify_otp"
    EXT_SEND_WHATSAPP_TEXT = "madad_external_send_whatsapp_text"
    EXT_SEND_WHATSAPP_TEMPLATE = "madad_external_send_whatsapp_template"
    EXT_SEND_WHATSAPP_INTERACTIVE = "madad_external_send_whatsapp_interactive"
    EXT_SEND_WHATSAPP_INTERACTIVE_BUTTONS = (
        "madad_external_send_whatsapp_interactive_buttons"
    )
    EXT_SEND_WHATSAPP_DOCUMENT = "madad_external_send_whatsapp_document"

    # -- MCP-side orchestration ----------------------------------------------
    MCP_CREATE_CHANNEL_SESSION = "madad_mcp_create_channel_session"
    MCP_GET_WEBHOOK_EVENTS = "madad_mcp_get_webhook_events"
    MCP_EMIT_WEBHOOK = "madad_mcp_emit_webhook"
    # Per Ishan (2026-06-07): WhatsApp lead onboarding-step tracking. Backend
    # hard-gates the pre-qualified document checklist on step >= 3 — we MUST
    # call ``update_onboarding_progress(step=3)`` after the audited financial
    # report upload + account.created message. ``get`` is the resume read.
    MCP_UPDATE_ONBOARDING_PROGRESS = "madad_mcp_update_onboarding_progress"
    MCP_GET_ONBOARDING_PROGRESS = "madad_mcp_get_onboarding_progress"
    # Per Ishan (cluster commit e6ea5d2, 2026-06-10): read-only existing-user
    # lookup. Called BEFORE ``create_channel_session`` on the campaign-entry
    # path so a returning user is routed (re-send offer, payment link,
    # invoice prompt, portal login, etc.) instead of being silently re-
    # onboarded. Closes Bug #2 + Bug #6.
    MCP_CHECK_REGISTRATION = "madad_mcp_check_registration"
    # Business-email capture step (right after YES). Backend returns
    # {ok, conflict, alreadyPortalUser}: conflict -> ask for a different email /
    # contact support; otherwise attach the email (also makes the lead a
    # normal, portal-loginable user) and continue to the CR step.
    MCP_SET_BUSINESS_EMAIL = "madad_mcp_set_business_email"

    # -- KYC (30; ``madad_kyc_complete_stage`` deliberately omitted) ---------
    KYC_UPDATE_ELIGIBILITY = "madad_kyc_update_eligibility"
    KYC_UPLOAD_DOCUMENT = "madad_kyc_upload_document"
    KYC_UPLOAD_DOCUMENT_BASE64 = "madad_kyc_upload_document_base64"
    # Per Ishan (2026-06-07): the classify-and-upload tools auto-detect the
    # document type using Madad's portal classifier. Preferred for
    # WhatsApp/email uploads where the SME doesn't know (or doesn't say)
    # which doc type they're sending — eliminates our filename-keyword
    # inference and routes everything through the same backend pipeline
    # the MSME complete-onboarding page uses.
    KYC_CLASSIFY_AND_UPLOAD_DOCUMENT_BASE64 = "madad_kyc_classify_and_upload_document_base64"
    KYC_CLASSIFY_DOCUMENT_BASE64 = "madad_kyc_classify_document_base64"
    KYC_CLASSIFY_AND_UPLOAD_ZIP_BASE64 = "madad_kyc_classify_and_upload_zip_base64"
    KYC_UPLOAD_INVOICE_BASE64 = "madad_kyc_upload_invoice_base64"
    KYC_UPLOAD_COMMERCIAL_REGISTRATION = "madad_kyc_upload_commercial_registration"
    KYC_UPLOAD_AUDITED_FINANCIAL_REPORT = "madad_kyc_upload_audited_financial_report"
    KYC_INSPECT_ZIP = "madad_kyc_inspect_zip"
    KYC_UPLOAD_ZIP_DOCUMENTS = "madad_kyc_upload_zip_documents"
    KYC_DELETE_DOCUMENT = "madad_kyc_delete_document"
    KYC_GET_DOCUMENTS = "madad_kyc_get_documents"
    KYC_GET_BUSINESS_DOCUMENTS = "madad_kyc_get_business_documents"
    KYC_GET_FINANCIAL_DOCUMENTS = "madad_kyc_get_financial_documents"
    KYC_GET_ADMIN_REQUESTED_DOCUMENTS = "madad_kyc_get_admin_requested_documents"
    KYC_GET_BUSINESS_DETAILS = "madad_kyc_get_business_details"
    KYC_UPDATE_BUSINESS_DETAILS = "madad_kyc_update_business_details"
    KYC_UPDATE_PRIMARY_DETAILS = "madad_kyc_update_primary_details"
    KYC_UPDATE_SECTOR_DETAIL = "madad_kyc_update_sector_detail"
    KYC_ADD_SHAREHOLDERS = "madad_kyc_add_shareholders"
    KYC_GET_SHAREHOLDERS = "madad_kyc_get_shareholders"
    KYC_GET_SHAREHOLDERS_KYC_STATUS = "madad_kyc_get_shareholders_kyc_status"
    KYC_GET_SHAREHOLDERS_CONSENTS = "madad_kyc_get_shareholders_consents"
    KYC_UPDATE_SHAREHOLDER = "madad_kyc_update_shareholder"
    KYC_DELETE_SHAREHOLDER = "madad_kyc_delete_shareholder"
    KYC_UPLOAD_SHAREHOLDER_DOCUMENTS = "madad_kyc_upload_shareholder_documents"
    KYC_ADD_BUYER = "madad_kyc_add_buyer"
    KYC_GET_BUYERS = "madad_kyc_get_buyers"
    KYC_EDIT_BUYER = "madad_kyc_edit_buyer"
    KYC_DELETE_BUYER = "madad_kyc_delete_buyer"
    KYC_GET_PRIMARY_BANK_DETAIL = "madad_kyc_get_primary_bank_detail"

    # -- offers (read-only) --------------------------------------------------
    OFFERS_GET_MY_OFFERS = "madad_offers_get_my_offers"
    OFFERS_GET_OFFER = "madad_offers_get_offer"
    OFFERS_GET_OFFERS = "madad_offers_get_offers"
    OFFERS_GET_EXTENSION_REQUEST = "madad_offers_get_extension_request"

    # -- TESS monetization payments (write tools require idempotency_key) ----
    PAYMENTS_LIST_MONETIZATION_PRODUCTS = "madad_payments_list_monetization_products"
    PAYMENTS_CREATE_MONETIZATION_PAYMENT = "madad_payments_create_monetization_payment"
    PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK = "madad_payments_send_monetization_payment_link"
    PAYMENTS_GET_MONETIZATION_PAYMENT = "madad_payments_get_monetization_payment"
    PAYMENTS_SYNC_MONETIZATION_PAYMENT_STATUS = "madad_payments_sync_monetization_payment_status"

    # -- invoices (Phase 1.b; deployed but not used by Phase 1.a) ------------
    INVOICES_GET_MY_INVOICES = "madad_invoices_get_my_invoices"
    INVOICES_EXTRACT_INVOICE = "madad_invoices_extract_invoice"
    INVOICES_EXTRACT_INVOICE_BASE64 = "madad_invoices_extract_invoice_base64"
    INVOICES_SUBMIT_INVOICE = "madad_invoices_submit_invoice"
    INVOICES_SUBMIT_INVOICE_BASE64 = "madad_invoices_submit_invoice_base64"
    INVOICES_EXTRACT_AND_SUBMIT_INVOICE = "madad_invoices_extract_and_submit_invoice"
    INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64 = "madad_invoices_extract_and_submit_invoice_base64"
    INVOICES_INSPECT_ZIP = "madad_invoices_inspect_zip"
    INVOICES_UPLOAD_ZIP = "madad_invoices_upload_zip"

    # -- helpers -------------------------------------------------------------

    @classmethod
    def all(cls) -> dict[str, str]:
        """All registered tool constants as a ``{constant_name: tool_string}`` map."""

        return {
            key: value
            for key, value in vars(cls).items()
            if key.isupper() and isinstance(value, str)
        }

    @classmethod
    def read_only(cls) -> set[str]:
        """Tool strings that never mutate backend state — always safe to retry."""

        prefixes = (
            "madad_auth_me",
            "madad_auth_check_contact",
            "madad_auth_google_oauth_url",
            "madad_kyc_get_",
            "madad_kyc_inspect_zip",
            "madad_offers_get_",
            "madad_payments_get_",
            "madad_payments_list_",
            "madad_invoices_get_",
            # UAT 2026-06-20: ``madad_invoices_extract_invoice*`` was here.
            # The OCR call routinely takes 60-235 seconds; the first attempt
            # almost always wins eventually. When the upstream bridge times
            # out and we retry, we just pile a second 200+ second wait on
            # top of the first — never improves the outcome, just compounds
            # the latency and load on Madad's OCR. Single-shot is correct
            # here. Same logic for the ``and_submit`` variant (already
            # single-shot because it's a write).
            "madad_invoices_inspect_zip",
            "madad_mcp_get_webhook_events",
        )
        return {
            value
            for value in cls.all().values()
            if any(value.startswith(p) or value == p for p in prefixes)
        }

    @classmethod
    def payment_idempotent_writes(cls) -> set[str]:
        """Payment write tools that accept an ``idempotency_key`` parameter.

        These are safe to retry transparently when the same key is reused.
        """

        return {
            cls.PAYMENTS_CREATE_MONETIZATION_PAYMENT,
            cls.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK,
        }
