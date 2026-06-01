"""The tool registry is the single source of provisional tool names."""

from __future__ import annotations

from app.shared.mcp import Tools


def test_registry_lists_all_constants():
    names = Tools.all()
    # Every capability the platform integrates is represented.
    assert {
        "WHATSAPP_SEND",
        "EMAIL_SEND",
        "DOCUMENT_PROCESS",
        "DOCUMENT_CHECKLIST",
        "ELIGIBILITY_CHECK",
        "PREQUAL_REQUEST",
        "SCORE_REQUEST",
        "LENDERS_SUBMIT",
        "CREDITLINE_ACTIVATE",
        "PAYMENT_CREATE_LINK",
    } <= set(names)


def test_tool_names_are_nonempty_and_unique():
    values = list(Tools.all().values())
    assert all(v for v in values)
    assert len(values) == len(set(values))  # no duplicate tool strings
