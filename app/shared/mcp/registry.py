"""Central registry of MCP tool names — ONE place to reconcile with the catalog.

CATALOG-DEPENDENT: every name below is PROVISIONAL pending Ishan's MCP tool
catalog. When it arrives, update the names here (and confirm payload shapes in the
adapters that reference them); nothing else should need to change.
"""

from __future__ import annotations


class Tools:
    """Provisional MCP tool-name constants, grouped by capability."""

    # -- channels (Communication service) ------------------------------------
    WHATSAPP_SEND = "whatsapp.send_message"
    EMAIL_SEND = "email.send_message"

    # -- documents (Madad extraction microservice) ---------------------------
    DOCUMENT_PROCESS = "madad.documents.process"
    DOCUMENT_CHECKLIST = "madad.documents.checklist"

    # -- Madad backend (financing decisions) ---------------------------------
    ELIGIBILITY_CHECK = "madad.eligibility.check"
    PREQUAL_REQUEST = "madad.prequalification.request"
    SCORE_REQUEST = "madad.score.request"
    LENDERS_SUBMIT = "madad.lenders.submit"
    CREDITLINE_ACTIVATE = "madad.creditline.activate"

    # -- payments (Tess) -----------------------------------------------------
    PAYMENT_CREATE_LINK = "tess.payment.create_link"

    @classmethod
    def all(cls) -> dict[str, str]:
        """All registered tool constants (name -> tool string)."""

        return {
            key: value
            for key, value in vars(cls).items()
            if key.isupper() and isinstance(value, str)
        }
