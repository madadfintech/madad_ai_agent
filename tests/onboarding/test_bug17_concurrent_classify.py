"""Bug #17 (UAT 2026-06-09): sequential per-file classify exceeded the
workflow's 60s step_timeout when one file was slow.

UAT trace (run_019ead92ae1192f10a621d78):
  18:17:34 — SME uploaded docs.zip (multiple files)
  18:18:02 — classify_and_upload.failed for aoa.pdf (timeout @ 25s)
  18:19:07 — classify_and_upload.failed for aoa.pdf (LangGraph retry #1)
  18:20:10 — classify_and_upload.failed for aoa.pdf (LangGraph retry #2)
  18:20:35 — workflow.run.failed: RetryExhaustedError × 3 → TIMED_OUT

Per-file processing was sequential, so cumulative wall-clock blew past
the 60s ``step_timeout_seconds`` budget when one file (AoA) hit the 25s
asyncio.wait_for cap. The workflow runtime then retried the WHOLE node
from the last checkpoint — same docs.zip, same per-file processing,
same timeout. After 3 retries the run died.

Concurrent processing with ``asyncio.gather`` caps wall-clock at the
slowest single file (≈ 25s) regardless of batch size, so a slow / hung
backend on ONE file can't take the rest of the batch down with it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services.workflow.onboarding import OnboardingWorkflow
from app.services.workflow.ports import InMemoryKycClient
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501701"
DOC = "ZHVtbXk="


def test_classify_loop_uses_asyncio_gather() -> None:
    """White-box invariant: the per-file classify loop must use
    ``asyncio.gather`` (or equivalent concurrent primitive). A future
    revert to sequential ``for ... await`` would re-introduce the
    cumulative-timeout pathology that killed run_019ead92ae1192f10a621d78."""
    import inspect

    source = inspect.getsource(
        OnboardingWorkflow._documents_upload_loop_await
    )
    assert "asyncio.gather" in source, (
        "per-file classify must run concurrently (Bug #17). Found no "
        "asyncio.gather in _documents_upload_loop_await — sequential "
        "processing has been reintroduced."
    )


class _SlowClassifier(InMemoryKycClient):
    """Records call timings so the test can prove the per-file calls
    actually ran in parallel (not serialised)."""

    def __init__(
        self,
        *,
        slow_files: dict[str, float],
        required_documents: list[str],
    ) -> None:
        super().__init__(required_documents=required_documents)
        self._slow_files = slow_files
        self.start_times: dict[str, float] = {}
        self.end_times: dict[str, float] = {}

    async def classify_and_upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        document_param: str | None = None,
        document_label: str | None = None,
    ) -> dict[str, Any]:
        self.start_times[filename] = time.monotonic()
        delay = self._slow_files.get(filename, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        result = await super().classify_and_upload_document_base64(
            access_token=access_token,
            content_base64=content_base64,
            filename=filename,
            mime_type=mime_type,
            document_param=document_param,
            document_label=document_label,
        )
        self.end_times[filename] = time.monotonic()
        return result


async def test_per_file_calls_overlap_in_time(harness) -> None:
    """Drive four slow uploads through the docs loop. Each call sleeps
    0.5s; wall-clock for the whole batch must be well under 4 × 0.5s
    (the sequential lower bound), proving the per-file calls actually
    overlapped via asyncio.gather."""

    # Swap the harness's KYC client out for the slow stand-in so we
    # can measure the parallelism end-to-end.
    slow_kyc = _SlowClassifier(
        slow_files={
            "aoa.pdf": 0.5,
            "establishment.pdf": 0.5,
            "bank.pdf": 0.5,
            "qid.pdf": 0.5,
        },
        required_documents=["x"],
    )
    harness.platform.workflow._kyc = slow_kyc  # type: ignore[assignment]

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
    )

    # Reset timings so the prior CR + Audited uploads don't pollute the
    # measurement.
    slow_kyc.start_times.clear()
    slow_kyc.end_times.clear()

    # Four files in one batch.
    t0 = time.monotonic()
    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {"filename": "aoa.pdf", "content_base64": DOC},
                {"filename": "establishment.pdf", "content_base64": DOC},
                {"filename": "bank.pdf", "content_base64": DOC},
                {"filename": "qid.pdf", "content_base64": DOC},
            ]
        },
    )
    wall = time.monotonic() - t0

    # Sequential would take ≥ 4 × 0.5 = 2.0s. Concurrent should be
    # well under that — give a generous bound so CI scheduling jitter
    # doesn't flake the test.
    assert wall < 1.5, (
        f"per-file classify took {wall:.2f}s — should be concurrent and "
        f"finish in roughly max(per-file) not sum(per-file)"
    )
    # And every file's call ACTUALLY ran.
    for fn in ("aoa.pdf", "establishment.pdf", "bank.pdf", "qid.pdf"):
        assert fn in slow_kyc.start_times, (
            f"classify for {fn} never ran — gather skipped it?"
        )
