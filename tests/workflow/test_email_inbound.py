"""Tests for the email inbound bridge.

Two layers:
1. **Parser** (``SendgridInboundParser``) — header parsing, sender
   extraction, attachment base64ing, threading-header capture.
2. **Helpers** (``parse_sender_address``, ``parse_message_id_header``,
   ``_crude_html_to_text``) — small functions worth pinning so a future
   refactor can't silently regress sender parsing.

Route-level integration is exercised through the workflow service's
TestClient in ``test_email_inbound_route.py``.
"""

from __future__ import annotations

import base64

from app.services.workflow.email_inbound import (
    SendgridInboundParser,
    _crude_html_to_text,
    parse_message_id_header,
    parse_sender_address,
    to_inbound_request_dict,
)
from app.shared.workflow.enums import Channel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestParseSenderAddress:
    def test_full_rfc5322_with_quoted_name(self) -> None:
        assert parse_sender_address('"Sara Khan" <sara@example.qa>') == "sara@example.qa"

    def test_rfc5322_unquoted_name(self) -> None:
        assert parse_sender_address("Sara Khan <sara@example.qa>") == "sara@example.qa"

    def test_bare_address(self) -> None:
        assert parse_sender_address("sara@example.qa") == "sara@example.qa"

    def test_lowercases(self) -> None:
        assert parse_sender_address("Sara@Example.QA") == "sara@example.qa"

    def test_angle_only(self) -> None:
        assert parse_sender_address("<sara@example.qa>") == "sara@example.qa"

    def test_empty_returns_none(self) -> None:
        assert parse_sender_address("") is None
        assert parse_sender_address(None) is None

    def test_no_at_returns_none(self) -> None:
        assert parse_sender_address("not-an-email") is None


class TestParseMessageIdHeader:
    def test_with_brackets(self) -> None:
        assert parse_message_id_header("<abc.123@mail.example>") == "abc.123@mail.example"

    def test_without_brackets(self) -> None:
        assert parse_message_id_header("abc.123@mail.example") == "abc.123@mail.example"

    def test_strips_whitespace(self) -> None:
        assert parse_message_id_header("  <a@b>  ") == "a@b"

    def test_empty_returns_none(self) -> None:
        assert parse_message_id_header(None) is None
        assert parse_message_id_header("") is None


class TestCrudeHtmlToText:
    def test_strips_tags(self) -> None:
        out = _crude_html_to_text("<p>Hello <b>Sara</b>!</p>")
        assert out == "Hello Sara !"   # spaces preserved where tags lived

    def test_collapses_whitespace(self) -> None:
        out = _crude_html_to_text("<p>a\n\n\nb</p>")
        assert "  " not in out


# ---------------------------------------------------------------------------
# SendgridInboundParser
# ---------------------------------------------------------------------------


def _build_parser(form: dict, files: dict | None = None) -> SendgridInboundParser:
    return SendgridInboundParser(form=form, files=files or {})


class TestSendgridParser:
    def test_happy_path_text_only(self) -> None:
        parser = _build_parser({
            "from": '"Sara" <sara@example.qa>',
            "to": "ops@madadfintech.com",
            "subject": "Re: Your application",
            "text": "Hi, please find my CR attached.",
            "headers": (
                "From: Sara <sara@example.qa>\n"
                "To: ops@madadfintech.com\n"
                "Subject: Re: Your application\n"
                "Message-ID: <inbound.001@example.qa>\n"
                "In-Reply-To: <outbound.001@madad>\n"
            ),
        })
        out = parser.parse()
        assert out is not None
        assert out.sender == "sara@example.qa"
        assert out.text == "Hi, please find my CR attached."
        assert out.subject == "Re: Your application"
        assert out.to == "ops@madadfintech.com"
        assert out.message_id == "inbound.001@example.qa"
        assert out.in_reply_to == "outbound.001@madad"

    def test_html_only_fallback(self) -> None:
        parser = _build_parser({
            "from": "sara@example.qa",
            "html": "<p>Hi <b>team</b>!</p>",
            "headers": "From: sara@example.qa\n",
        })
        out = parser.parse()
        assert out is not None
        assert "Hi" in (out.text or "")
        assert "team" in (out.text or "")

    def test_attachments_base64ed(self) -> None:
        attach_bytes = b"%PDF-1.7\nfake-cr-bytes\n"
        parser = _build_parser(
            form={
                "from": "sara@example.qa",
                "subject": "CR",
                "text": "attached",
                "headers": "From: sara@example.qa\n",
                "attachments": "1",
                "attachment-info": (
                    '{"attachment1": {"filename": "CR.pdf", "type": "application/pdf"}}'
                ),
            },
            files={"attachment1": ("CR.pdf", attach_bytes, "application/pdf")},
        )
        out = parser.parse()
        assert out is not None
        assert len(out.attachments) == 1
        att = out.attachments[0]
        assert att["filename"] == "CR.pdf"
        assert att["mime_type"] == "application/pdf"
        decoded = base64.b64decode(att["content_base64"])
        assert decoded == attach_bytes

    def test_no_sender_returns_none(self) -> None:
        parser = _build_parser({"text": "no from"})
        assert parser.parse() is None

    def test_sender_from_envelope_fallback(self) -> None:
        parser = _build_parser({
            "envelope": '{"from": "envelope-sara@example.qa", "to": ["x"]}',
            "text": "hi",
        })
        out = parser.parse()
        assert out is not None
        assert out.sender == "envelope-sara@example.qa"

    def test_multi_references_chain(self) -> None:
        parser = _build_parser({
            "from": "sara@example.qa",
            "headers": (
                "From: sara@example.qa\n"
                "References: <a@b> <c@d> <e@f>\n"
            ),
        })
        out = parser.parse()
        assert out is not None
        assert out.references == ["a@b", "c@d", "e@f"]


# ---------------------------------------------------------------------------
# Translation to InboundRequest dict
# ---------------------------------------------------------------------------


class TestToInboundRequestDict:
    def test_basic_translation(self) -> None:
        from app.services.workflow.email_inbound import ParsedInboundEmail

        parsed = ParsedInboundEmail(
            sender="sara@example.qa",
            text="hello",
            subject="hi",
            attachments=[{"filename": "x.pdf",
                          "content_base64": "ZHVtbXk=",
                          "mime_type": "application/pdf"}],
            message_id="m1@x",
            in_reply_to="o1@madad",
        )
        out = to_inbound_request_dict(parsed)
        assert out["channel"] is Channel.EMAIL
        assert out["identity"] == "sara@example.qa"
        assert out["text"] == "hello"
        assert out["attachments"][0]["filename"] == "x.pdf"
        assert out["message_id"] == "m1@x"
        assert out["data"]["email_in_reply_to"] == "o1@madad"
        assert out["data"]["email_subject"] == "hi"
