"""Inbound email bridge — translates ESP webhook payloads into ``InboundRequest``.

The workflow service already exposes ``POST /workflow/inbound`` as the
single chokepoint for SME-originated messages. To accept inbound emails
we need a thin translator that:

1. Accepts the webhook shape an Email Service Provider (ESP) emits when
   an SME replies (forwards a parsed email to a configured URL).
2. Extracts ``identity`` (sender), ``text`` (body), ``attachments``
   (base64-encoded), and threading hints (``In-Reply-To`` /
   ``References``).
3. Forwards as an :class:`InboundRequest` to the standard inbound flow.

Why a separate module
---------------------

* The parsing is provider-specific — SendGrid Inbound Parse, Mailgun
  Routes, AWS SES → SNS, Postmark, etc. all use slightly different
  shapes. Keeping the translation here lets us add new providers as
  parallel ``Sendgrid…Parser`` / ``Mailgun…Parser`` classes without
  touching the route or the workflow service.
* PI-2 ships ONE concrete translator (SendGrid Inbound Parse —
  the most common shape; Mailgun is API-compatible). Additional ESPs
  add a class here + a route in ``main.py``.

What this module ISN'T
----------------------

* Not a Meta WhatsApp inbound bridge — WhatsApp inbound flows via
  Madad's existing bridge that already POSTs to ``/workflow/inbound``
  directly.
* Not an email PROVIDER — the SEND path uses Ishan's
  ``madad_external_send_email_text`` MCP tool. We are the RECEIVE
  side only.
* Not a thread-matcher. Threading lookup (``In-Reply-To`` → existing
  conversation) is captured as metadata for now; matching to the
  right run still uses ``(channel, identity)`` — the SME's email
  address.
"""

from __future__ import annotations

import base64
import email.utils
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.shared.workflow.enums import Channel

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ANGLE_BRACKETS_RE = re.compile(r"<([^<>]+)>")


def parse_sender_address(raw: str | None) -> str | None:
    """Extract the bare email address from an RFC 5322 ``From:`` line.

    Handles all three common shapes:
        ``"Sara Khan" <sara@example.qa>``
        ``Sara Khan <sara@example.qa>``
        ``sara@example.qa``

    Falls back to ``None`` for unparseable input — the route will
    return HTTP 400 rather than dispatch with a bad identity.
    """
    if not raw:
        return None
    raw = raw.strip()
    name, addr = email.utils.parseaddr(raw)
    if addr and "@" in addr:
        return addr.lower()
    # Last-ditch: pull anything looking like an address.
    m = _ANGLE_BRACKETS_RE.search(raw)
    if m and "@" in m.group(1):
        return m.group(1).lower()
    if "@" in raw:
        return raw.lower()
    return None


def parse_message_id_header(raw: str | None) -> str | None:
    """Extract a Message-ID from an ``In-Reply-To`` or ``Message-ID`` header.

    These headers carry the ID wrapped in angle brackets per RFC 5322::

        In-Reply-To: <abc.123@mail.example>

    Returns the bare ID (without brackets) or ``None``.
    """
    if not raw:
        return None
    m = _ANGLE_BRACKETS_RE.search(raw)
    if m:
        return m.group(1).strip()
    s = raw.strip()
    return s or None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass
class ParsedInboundEmail:
    """Wire-format-agnostic representation of one inbound email.

    Each concrete ESP parser returns this shape; the route then
    constructs an :class:`InboundRequest` from it.
    """

    sender: str
    """Bare email address (lower-cased) — becomes ``identity``."""

    text: str | None
    """Plain-text body. ``None`` only if the email was HTML-only AND
    the parser couldn't down-convert."""

    attachments: list[dict[str, Any]] = field(default_factory=list)
    """Attachments in the ``{filename, content_base64, mime_type}``
    shape the agent's document nodes already consume from WhatsApp
    inbounds."""

    message_id: str | None = None
    """ESP-assigned ``Message-ID`` for THIS inbound email — surfaces
    on the workflow inbound's ``message_id`` field for dedup."""

    in_reply_to: str | None = None
    """``In-Reply-To`` header — points at the agent's prior outbound.
    Captured for future thread-matching."""

    references: list[str] = field(default_factory=list)
    """``References`` header chain — same future use."""

    subject: str | None = None
    """Email subject — captured on the inbound metadata for the
    AEGIS Comms Review surfaces."""

    to: str | None = None
    """``To:`` — the WABA / mailbox address this email landed at.
    Useful when one workflow accepts multiple inboxes."""

    raw_headers: dict[str, str] = field(default_factory=dict)


class SendgridInboundParser:
    """Translate a SendGrid Inbound Parse webhook into a :class:`ParsedInboundEmail`.

    SendGrid posts ``multipart/form-data`` with these fields (the ones we
    actually consume):

        from               — RFC 5322 mailbox (``"Sara" <sara@x.qa>``)
        to                 — destination mailbox
        subject            — subject line
        text               — plain-text body (always present unless the
                              email was HTML-only and the sender's MUA
                              didn't include a plain-text alternative)
        html               — HTML body (fallback when ``text`` is missing)
        headers            — raw header block (one header per line)
        attachments        — integer count, like "2"
        attachment-info    — JSON dict keyed by ``attachment1``,
                              ``attachment2``, … with ``filename`` /
                              ``type`` per entry
        attachment1…N      — file parts (we read as bytes, base64-encode)
        envelope           — JSON ``{from: "x", to: ["y"]}`` (fallback
                              when the ``from``/``to`` fields are missing)

    Mailgun's Routes webhook is structurally identical; this parser
    works against either with no changes.

    Construction is cheap — instantiate per request.
    """

    def __init__(
        self,
        *,
        form: dict[str, str],
        files: dict[str, tuple[str, bytes, str | None]] | None = None,
    ) -> None:
        self._form = form
        self._files = files or {}

    def parse(self) -> ParsedInboundEmail | None:
        """Return the parsed email, or ``None`` if the payload is
        too malformed to dispatch (no sender)."""
        sender = self._extract_sender()
        if sender is None:
            _LOG.warning("email_inbound.sendgrid: no sender; dropping")
            return None
        text = self._extract_text()
        attachments = self._extract_attachments()
        headers = self._parse_headers_block(self._form.get("headers"))
        return ParsedInboundEmail(
            sender=sender,
            text=text,
            attachments=attachments,
            message_id=parse_message_id_header(headers.get("message-id")),
            in_reply_to=parse_message_id_header(headers.get("in-reply-to")),
            references=self._parse_references(headers.get("references")),
            subject=(self._form.get("subject") or "").strip() or None,
            to=parse_sender_address(self._form.get("to")) or None,
            raw_headers=headers,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _extract_sender(self) -> str | None:
        addr = parse_sender_address(self._form.get("from"))
        if addr:
            return addr
        # Envelope fallback — SendGrid sends a JSON-encoded envelope
        # field when the From mailbox is missing or anonymous.
        envelope_raw = self._form.get("envelope")
        if envelope_raw:
            try:
                import json
                env = json.loads(envelope_raw)
                if isinstance(env, dict):
                    return parse_sender_address(env.get("from"))
            except (ValueError, TypeError):
                pass
        return None

    def _extract_text(self) -> str | None:
        text = (self._form.get("text") or "").strip()
        if text:
            return text
        # HTML-only fallback: lazy import a minimal stripper so we don't
        # depend on beautifulsoup. A future variant could call out to a
        # full HTML-to-text converter; for the M1 demo, the plain-text
        # alternative is almost always present.
        html = self._form.get("html")
        if html:
            return _crude_html_to_text(html)
        return None

    def _extract_attachments(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        info_map = self._parse_attachment_info(self._form.get("attachment-info"))
        # SendGrid keys attachments ``attachment1`` … ``attachmentN``.
        for key, file_tuple in self._files.items():
            filename, content, mime_type = file_tuple
            info = info_map.get(key, {})
            attachment_filename = info.get("filename") or filename or key
            attachment_mime = (
                info.get("type")
                or mime_type
                or "application/octet-stream"
            )
            try:
                content_b64 = base64.b64encode(content).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "email_inbound.sendgrid: cannot base64 attachment %s: %s",
                    key, str(exc)[:200],
                )
                continue
            out.append({
                "filename": attachment_filename,
                "content_base64": content_b64,
                "mime_type": attachment_mime,
            })
        return out

    @staticmethod
    def _parse_attachment_info(raw: str | None) -> dict[str, dict[str, Any]]:
        if not raw:
            return {}
        try:
            import json
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _parse_headers_block(raw: str | None) -> dict[str, str]:
        """Parse the ``headers`` form-field (raw RFC 5322 headers) into
        a lowercase-key dict.

        SendGrid concatenates the full header block (one header per
        line, with continuation handling already done). We don't need
        rigorous RFC parsing — we only consume a handful of fields.
        """
        if not raw:
            return {}
        out: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line or line.startswith(" ") or line.startswith("\t"):
                continue
            key, _, val = line.partition(":")
            out[key.strip().lower()] = val.strip()
        return out

    @staticmethod
    def _parse_references(raw: str | None) -> list[str]:
        """``References:`` is a space-separated chain of Message-IDs."""
        if not raw:
            return []
        out: list[str] = []
        for m in _ANGLE_BRACKETS_RE.finditer(raw):
            mid = m.group(1).strip()
            if mid:
                out.append(mid)
        return out


# ---------------------------------------------------------------------------
# Crude HTML stripper (fallback when SendGrid only delivers HTML)
# ---------------------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _crude_html_to_text(html: str) -> str:
    """Best-effort HTML → plain text. Not Markdown; not pretty.

    Strips tags, collapses whitespace. Good enough to get the SME's
    typed reply out of a rich-text email. Real HTML-aware extraction
    is deferred to a follow-up.
    """
    no_tags = _TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


# ---------------------------------------------------------------------------
# Translation to InboundRequest shape
# ---------------------------------------------------------------------------


def to_inbound_request_dict(parsed: ParsedInboundEmail) -> dict[str, Any]:
    """Build the kwargs an :class:`InboundRequest` accepts.

    Kept as a plain ``dict`` so the route can construct the request
    without coupling this module to the route's Pydantic model.
    """
    data: dict[str, Any] = {}
    if parsed.subject:
        data.setdefault("email_subject", parsed.subject)
    if parsed.to:
        data.setdefault("email_to", parsed.to)
    if parsed.in_reply_to:
        data.setdefault("email_in_reply_to", parsed.in_reply_to)
    if parsed.references:
        data.setdefault("email_references", parsed.references)
    return {
        "channel": Channel.EMAIL,
        "identity": parsed.sender,
        "text": parsed.text,
        "attachments": list(parsed.attachments),
        "data": data,
        "message_id": parsed.message_id,
    }
