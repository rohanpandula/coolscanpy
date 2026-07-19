"""The roll-feeder extension: :class:`Roll`.

Wires three concrete subsystems that exist independently in this package but
were never previously composed into one whole-roll workflow:

* :mod:`coolscanpy.roll.preview_session` -- decodes one whole-roll transport
  read into fixed-order slots, spacing-offset math, and the reviewed
  fingerprint;
* :mod:`coolscanpy.protocol.ls5000_single_pass.capture_process` --
  ``CaptureProcessAdapter``, the process-isolated batch capture engine (the
  same class the concrete test suite already drives through injectable
  ``runner``/``batch_spawner`` seams, per the hardware-free testing
  contract);
* :mod:`coolscanpy.capture.single_pass_workflow` -- decodes, quality-checks,
  and finalizes one completed attempt.

Only ``Material.COLOR_NEGATIVE``'s single-pass RGBI4 capture route
(``CaptureRoute.SINGLE_PASS_RGBI4``) is wired end to end here. See the
``scan_many`` docstring and the package README's deviations note for why
``Material.BLACK_AND_WHITE_NEGATIVE``'s SANE-based route is not.
"""

from __future__ import annotations

import hashlib
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator
from uuid import uuid4

import numpy as np
import tifffile

from coolscanpy.capture.single_pass_workflow import (
    LS5000SinglePassWorkflow,
    SinglePassAttempt,
    SinglePassFinalizationResult,
    SinglePassSession,
    SinglePassWorkflowError,
)
from coolscanpy.exceptions import (
    BatchIntegrityError,
    FeederParked,
    FingerprintRefused,
    GeometryValidationError,
    ManualReviewRequired,
    PyCoolscanError,
    RefeedRequired,
    RollMismatch,
    SafeStopRequested,
    TransportSmearDetected,
)
from coolscanpy.protocol.ls5000_single_pass.capture_process import (
    BatchAckAction,
    CaptureBatchProcessError,
    CaptureBatchRequest,
    CaptureMode,
    CaptureOutcome,
    CaptureProcessAdapter,
    CaptureRequest,
    CaptureStopped,
)
from coolscanpy.roll.preview_session import (
    CaptureRoute,
    RollPreviewSession,
    build_roll_preview_session,
)
from coolscanpy.types import (
    ApprovalReceipt,
    ArtifactEvidence,
    ClippingTelemetry,
    ExposureVector,
    Frame,
    FingerprintComparison,
    FocusDetailTelemetry,
    Material,
    Progress,
    ProgressCallback,
    Receipt,
    RollFingerprint,
    Thumbnail,
    TransportSmearAssessment,
)

if TYPE_CHECKING:
    from coolscanpy._device import Device

RECEIPT_VERSION = 1
LS5000_FINE_DPI = 4_000
LS5000_FINE_DEPTH = 16

# Generous defensive backstop for winding down scan_many's worker thread --
# not a hardware operation timeout (a real fine-scan frame is never
# abandoned mid-flight; see CaptureProcessAdapter's own docstring). Only
# matters if the worker is truly stuck, e.g. a hung child process.
_SCAN_WORKER_JOIN_TIMEOUT_SECONDS = 300.0

_BLACK_AND_WHITE_NOT_WIRED = (
    "Material.BLACK_AND_WHITE_NEGATIVE fine-scan capture is not wired in "
    "this package version: preview, spacing-offset, and approval all work "
    "for either material, but the SANE-based fine-scan pipeline "
    "(coolscanpy.roll.registration + coolscanpy.transport.sane.SaneBackend) "
    "has not been integrated with the roll batch engine. Only "
    "Material.COLOR_NEGATIVE's single-pass RGBI4 route "
    "(coolscanpy.protocol.ls5000_single_pass) is implemented end to end."
)


class Roll:
    """Owns the physical transport for one whole-roll batch.

    Acquired via :meth:`Device.roll`, released on :meth:`close`/context
    exit. Construct directly (rather than via ``Device.roll()``) only in
    tests that need to inject a ``CaptureProcessAdapter`` built with a fake
    ``runner``/``batch_spawner``, or an ``LS5000SinglePassWorkflow`` built
    with a fake decoder/quality assessor, mirroring the concrete test
    suite's own seams (see tests/test_facade.py).

    ``attempts_root`` selects where per-attempt evidence (the preview
    raster, transport table, journals, and capture scratch) is written.
    When omitted, a private temporary directory is created and removed on
    :meth:`close`. When supplied, the directory and everything written
    under it are preserved after :meth:`close`, so a refused preview or
    batch can be rerun and diagnosed offline without another hardware
    attempt.
    """

    def __init__(
        self,
        device: "Device",
        material: Material = Material.COLOR_NEGATIVE,
        *,
        adapter: CaptureProcessAdapter | None = None,
        workflow: LS5000SinglePassWorkflow | None = None,
        attempts_root: Path | None = None,
    ) -> None:
        self._device = device
        self._material = material
        self._session: RollPreviewSession | None = None
        self._approvals: dict[int, Any] = {}
        self._stop_event = threading.Event()
        self._closed = False
        self._owns_attempts_root = attempts_root is None
        self._attempts_root = (
            Path(attempts_root)
            if attempts_root is not None
            else Path(tempfile.mkdtemp(prefix="coolscanpy-roll-"))
        )
        self._transport_fault: tuple[type[PyCoolscanError], str] | None = None
        self._adapter = adapter
        self._workflow = workflow if workflow is not None else LS5000SinglePassWorkflow()

    # -- lifecycle -----------------------------------------------------

    @property
    def material(self) -> Material:
        return self._material

    @property
    def slot_count(self) -> int:
        """Fixed adapter capacity for this traversal, from the last
        :meth:`preview`."""

        return len(self._require_session().slots)

    def __enter__(self) -> "Roll":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotent. Ends the batch reservation.

        When this Roll created its own temporary attempts directory, close
        removes it. A caller-supplied ``attempts_root`` is preserved, along
        with every attempt directory written under it, so a failed preview
        or batch can be diagnosed offline from its persisted raster, table,
        and journal.
        """

        if self._closed:
            return
        self._closed = True
        if self._owns_attempts_root:
            shutil.rmtree(self._attempts_root, ignore_errors=True)
        self._device._release_roll_lock()

    # -- preview ---------------------------------------------------------

    def preview(
        self,
        slots: Iterable[int] | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[Thumbnail]:
        """One whole-roll transport read at ~97 dpi.

        Always a full-roll hardware read; ``slots`` only filters which
        Thumbnails are returned. Establishes this Roll's ``fingerprint``.
        Calling ``preview()`` again re-reads the transport, replaces the
        fingerprint and all Thumbnails, and clears any recorded approvals
        (they become stale).

        Raises ``RefeedRequired`` when the transport is parked at its
        end-stop, which an earlier preview traversal of a strip shorter
        than a full roll leaves behind. After that, or after
        ``FeederParked``, this Roll refuses every further hardware attempt:
        resolve the fault physically and open a fresh Roll.
        """

        self._require_open()
        self._refuse_if_transport_fault()
        adapter = self._ensure_adapter()
        if on_progress is not None:
            on_progress(
                Progress(
                    stage="preview",
                    slot=None,
                    index=0,
                    total=1,
                    fraction=0.0,
                    message="reading whole-roll transport index",
                )
            )
        request = CaptureRequest(mode=CaptureMode.PREVIEW)
        try:
            attempt = adapter.run_attempt(request)
        except CaptureStopped as error:
            raise SafeStopRequested(str(error)) from error

        if attempt.outcome is CaptureOutcome.RECOVERY_REQUIRED:
            raise self._record_transport_fault(
                FeederParked(
                    _journal_error(attempt)
                    or "scanner requires a power cycle before the next attempt"
                )
            )
        if attempt.outcome is not CaptureOutcome.COMPLETE:
            message = (
                _journal_error(attempt) or f"preview attempt did not complete: {attempt.outcome}"
            )
            if _is_end_stop_park(message):
                raise self._record_transport_fault(
                    RefeedRequired(
                        "the whole-roll index read was refused because the "
                        "transport is parked at its end-stop, most often left "
                        "there by an earlier preview traversal of a strip "
                        "shorter than a full roll; pull the strip fully out, "
                        "reinsert it until the feeder grips, then open a "
                        "fresh Roll"
                    )
                )
            raise PyCoolscanError(message)

        session = build_roll_preview_session(attempt, material=self._material)
        self._session = session
        self._approvals.clear()

        if on_progress is not None:
            on_progress(
                Progress(
                    stage="preview",
                    slot=None,
                    index=1,
                    total=1,
                    fraction=1.0,
                    message="preview complete",
                )
            )

        wanted = None if slots is None else set(slots)
        return [
            _thumbnail_from_slot(slot)
            for slot in session.slots
            if wanted is None or slot.slot_id in wanted
        ]

    # -- spacing offset ----------------------------------------------------

    def spacing_offset(self, slot: int) -> int:
        session = self._require_session()
        self._check_slot(session, slot)
        return session.slots[slot - 1].boundary_offset_rows

    def set_spacing_offset(self, slot: int, offset_rows: int) -> None:
        """Nudge ``slot``'s transport boundary by ``offset_rows`` native rows
        at preview resolution. Invalidates any existing approval for this
        slot."""

        session = self._require_session()
        self._check_slot(session, slot)
        self._session = session.with_boundary_offset(slot, offset_rows)
        self._approvals.pop(slot, None)

    # -- fingerprint ---------------------------------------------------

    @property
    def fingerprint(self) -> RollFingerprint:
        session = self._require_session()
        reviewed = session.reviewed_fingerprint()
        return RollFingerprint(
            sha256=reviewed.binding_sha256,
            slot_count=len(session.slots),
            preview_shape=reviewed.preview_shape,
        )

    # -- manual review / approval --------------------------------------

    def approve(self, slot: int) -> None:
        """Record approval of ``slot``'s current thumbnail and spacing
        offset. Raises ``ValueError`` if the slot doesn't need approval."""

        session = self._require_session()
        self._check_slot(session, slot)
        offset = session.slots[slot - 1].boundary_offset_rows
        approval = session.approve_manual_origin(slot, offset)
        self._approvals[slot] = approval

    def needs_approval(self, slot: int) -> bool:
        session = self._require_session()
        self._check_slot(session, slot)
        return session.slots[slot - 1].manual_review

    # -- scanning --------------------------------------------------------

    def scan(self, slot: int) -> Frame:
        """Fine-scan one slot. Sugar for ``next(iter(scan_many([slot])))``."""

        return next(iter(self.scan_many([slot])))

    def scan_many(
        self,
        slots: Iterable[int],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Iterator[Frame]:
        """One continuous transport reservation for the whole ordered
        ``slots`` list, yielding a Frame as each completes.

        The batch itself runs on a background worker thread; each decoded
        Frame crosses back to this iterator through a bounded queue (holding
        at most one frame at a time), so the caller receives frame N as soon
        as it is ready instead of waiting for the whole ``slots`` batch to
        finish. In-flight memory stays capped at about two frames (one
        queued, one the worker is finalizing) no matter how many slots are
        requested. Abandoning the iterator early (a ``break`` out of a
        ``for`` loop, or an explicit ``.close()``) requests the same safe
        stop as :meth:`safe_stop`, drains the queue, and joins the worker
        before propagating ``GeneratorExit`` -- it never leaves the worker
        blocked writing a finished frame to a queue nobody is reading.

        Only ``Material.COLOR_NEGATIVE`` (single-pass RGBI4) is implemented;
        a ``Material.BLACK_AND_WHITE_NEGATIVE`` Roll raises
        ``NotImplementedError`` here (see the module docstring). Argument
        validation (slot range, required approvals, material route) happens
        eagerly, before any hardware access; the actual batch runs lazily as
        the returned iterator is consumed.
        """

        self._require_open()
        self._refuse_if_transport_fault()
        session = self._require_session()
        ordered_slots = tuple(slots)
        if not ordered_slots:
            raise ValueError("scan_many requires at least one slot")
        for slot in ordered_slots:
            self._check_slot(session, slot)
        if session.recipe.capture_route is not CaptureRoute.SINGLE_PASS_RGBI4:
            raise NotImplementedError(_BLACK_AND_WHITE_NOT_WIRED)
        for slot in ordered_slots:
            if session.slots[slot - 1].manual_review and slot not in self._approvals:
                raise ManualReviewRequired(
                    f"slot {slot} uses an inferred transport origin; "
                    f"call approve({slot}) before scanning it",
                    slot=slot,
                )

        return self._scan_many(session, ordered_slots, on_progress)

    def _scan_many(
        self,
        session: RollPreviewSession,
        slots: tuple[int, ...],
        on_progress: ProgressCallback | None,
    ) -> Iterator[Frame]:
        adapter = self._ensure_adapter()
        requests = tuple(
            CaptureRequest(
                mode=CaptureMode.FULL,
                selected_slot=slot,
                boundary_offset_rows=session.slots[slot - 1].boundary_offset_rows,
                manual_review_approval=self._approvals.get(slot),
            )
            for slot in slots
        )
        batch_request = CaptureBatchRequest(
            frames=requests,
            reviewed_fingerprint=session.reviewed_fingerprint(),
        )
        single_pass_session = SinglePassSession(
            root=self._attempts_root,
            session_id=f"scan-{uuid4().hex}",
        )
        workflow = self._workflow
        device_id = self._device._info.id
        produced_count = 0

        # maxsize=1: the worker may finish decoding/finalizing at most one
        # frame beyond whatever is already queued before it blocks on
        # put(), which is exactly the "~2 frames in flight" bound this
        # queue exists to enforce.
        frame_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

        def frame_handler(attempt_result: Any) -> BatchAckAction:
            nonlocal produced_count
            single_pass_attempt = SinglePassAttempt.from_capture_result(
                session=single_pass_session,
                result=attempt_result,
            )
            try:
                finalization = workflow.finalize_attempt(single_pass_attempt)
            except SinglePassWorkflowError as error:
                raise _translate_finalization_error(error) from error
            frame = _read_frame(
                finalization,
                slot=attempt_result.request.selected_slot,
                device_id=device_id,
            )
            produced_count += 1
            if on_progress is not None:
                on_progress(
                    Progress(
                        stage="fine-scan",
                        slot=frame.slot,
                        index=produced_count - 1,
                        total=len(slots),
                        fraction=1.0,
                        message=f"slot {frame.slot} complete",
                    )
                )
            # Blocks until the consumer (or an abandonment drain) makes
            # room -- this, not a size counter, is the backpressure.
            frame_queue.put(("frame", frame))
            return BatchAckAction.STOP if self._stop_event.is_set() else BatchAckAction.CONTINUE

        def run_batch() -> None:
            try:
                try:
                    result = adapter.run_batch_session(batch_request, frame_handler=frame_handler)
                except CaptureStopped as error:
                    try:
                        raise SafeStopRequested(str(error)) from error
                    except SafeStopRequested as translated:
                        frame_queue.put(("error", translated))
                    return
                except CaptureBatchProcessError as error:
                    # frame_handler's own exceptions (including the typed
                    # ones frame_handler above already translates) arrive
                    # here wrapped, chained via `from handler_error` --
                    # unwrap back to the original typed exception rather
                    # than leaking the adapter's internal wrapper type.
                    cause = error.__cause__
                    try:
                        if isinstance(cause, PyCoolscanError):
                            raise cause from error
                        raise BatchIntegrityError(str(error)) from error
                    except BaseException as translated:
                        frame_queue.put(("error", translated))
                    return

                if result.outcome is CaptureOutcome.RECOVERY_REQUIRED:
                    frame_queue.put((
                        "error",
                        self._record_transport_fault(
                            FeederParked(
                                (result.session_journal or {}).get("error")
                                or "scanner requires a power cycle before the next attempt"
                            )
                        ),
                    ))
                    return
                if result.outcome is CaptureOutcome.SYNCHRONIZED_REFUSAL:
                    message = (result.session_journal or {}).get("error") or (
                        "roll batch was refused before any frame was captured"
                    )
                    if (
                        "does not match the reviewed roll fingerprint" in message
                        or "does not match its reviewed visual fingerprint" in message
                    ):
                        frame_queue.put((
                            "error",
                            FingerprintRefused(
                                message,
                                comparison=FingerprintComparison(
                                    matches=False,
                                    reason=message,
                                    compared_frames=0,
                                    visual_median_hamming=None,
                                    visual_p90_hamming=None,
                                    frame_start_median_delta_rows=None,
                                    frame_start_max_delta_rows=None,
                                ),
                            ),
                        ))
                        return
                    if "transport origin requires manual review" in message:
                        frame_queue.put(("error", ManualReviewRequired(message, slot=slots[0])))
                        return
                    if _is_end_stop_park(message):
                        # The fine-scan fresh index read's startup status came
                        # back non-zero: the transport is parked at its
                        # end-stop, most often left there by a preview
                        # traversal of a strip shorter than a full roll.
                        frame_queue.put((
                            "error",
                            self._record_transport_fault(
                                RefeedRequired(
                                    "the fine-scan fresh index read failed because "
                                    "the transport is parked at the end-stop from an "
                                    "earlier preview; pull the strip fully out, "
                                    "reinsert it until the feeder grips, then open a "
                                    "fresh Roll and preview again"
                                )
                            ),
                        ))
                        return
                    frame_queue.put(("error", RollMismatch(message)))
                    return

                frame_queue.put(("stopped", produced_count) if result.stopped else ("complete", None))
            except BaseException as error:  # pragma: no cover - safety net
                # Guarantees the consumer's queue.get() is never left
                # waiting forever for a terminal item that a genuinely
                # unexpected exception here would otherwise skip.
                frame_queue.put(("error", error))

        worker_thread = threading.Thread(
            target=run_batch,
            name="coolscanpy-roll-scan-many",
            daemon=True,
        )
        worker_thread.start()
        try:
            while True:
                kind, payload = frame_queue.get()
                if kind == "frame":
                    yield payload
                    continue
                if kind == "error":
                    raise payload
                if kind == "stopped":
                    raise SafeStopRequested(
                        f"safe stop requested; {payload} of {len(slots)} "
                        "requested frames completed"
                    )
                return  # kind == "complete"
        except GeneratorExit:
            self.safe_stop()
            _drain_abandoned_scan_queue(frame_queue, worker_thread)
            raise
        finally:
            worker_thread.join(timeout=_SCAN_WORKER_JOIN_TIMEOUT_SECONDS)

    # -- eject / safe stop -------------------------------------------------

    def safe_stop(self) -> None:
        """Request a graceful stop. The frame in flight always finishes;
        the next one raises ``SafeStopRequested`` instead of starting."""

        self._stop_event.set()

    def eject(self) -> bool:
        return self._device.eject()

    # -- internals -------------------------------------------------------

    def _ensure_adapter(self) -> CaptureProcessAdapter:
        if self._adapter is None:
            self._adapter = CaptureProcessAdapter.packaged(self._attempts_root)
        return self._adapter

    def _record_transport_fault(self, error: PyCoolscanError) -> PyCoolscanError:
        """Latch the first parked/wedged-transport fault this Roll observes.

        Returns ``error`` unchanged so refusal sites can raise or enqueue the
        recorded exception in one expression.
        """

        if self._transport_fault is None:
            self._transport_fault = (type(error), str(error))
        return error

    def _refuse_if_transport_fault(self) -> None:
        """Refuse further hardware attempts after a parked-transport fault.

        Retrying against a transport that refused its index read does not
        recover it; observed live, it escalates a recoverable park into a
        wedge that only a physical power cycle clears. The fault latches for
        this Roll's whole lifetime because the library cannot sense a refeed:
        the operator resolves the fault physically and opens a fresh Roll.
        """

        if self._transport_fault is None:
            return
        fault_type, original = self._transport_fault
        raise fault_type(
            "no hardware attempt was made: this Roll already observed a "
            f"transport fault ({original}); retrying against a parked "
            "transport can wedge it until a power cycle, so resolve the "
            "fault physically and open a fresh Roll"
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("this Roll has been closed")

    def _require_session(self) -> RollPreviewSession:
        self._require_open()
        if self._session is None:
            raise RuntimeError("preview() has not been called yet")
        return self._session

    @staticmethod
    def _check_slot(session: RollPreviewSession, slot: int) -> None:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= len(session.slots):
            raise ValueError(f"unknown roll slot: {slot!r}")


def _drain_abandoned_scan_queue(
    frame_queue: "queue.Queue[tuple[str, Any]]",
    worker_thread: threading.Thread,
) -> None:
    """Unblock a ``_scan_many`` worker stuck writing to a full queue after
    the consumer abandoned the iterator mid-batch (``GeneratorExit``).

    The caller has already requested a safe stop, so at most one more frame
    (already in flight) and then exactly one terminal marker are still
    coming; discard whatever frames arrive -- nobody wants them anymore --
    until the terminal marker appears or the bounded deadline passes.
    """

    deadline = time.monotonic() + _SCAN_WORKER_JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            kind, _payload = frame_queue.get(timeout=0.5)
        except queue.Empty:
            if not worker_thread.is_alive():
                return
            continue
        if kind != "frame":
            return


def _thumbnail_from_slot(slot: Any) -> Thumbnail:
    return Thumbnail(
        slot=slot.slot_id,
        image=slot.thumbnail,
        boundary_rows=slot.boundary_rows,
        spacing_offset=slot.boundary_offset_rows,
        needs_approval=slot.manual_review,
        warnings=slot.warnings,
    )


def _is_end_stop_park(message: str) -> bool:
    """Recognize the worker refusal left by a transport parked at its
    end-stop: the whole-roll or fine-scan index read's startup status came
    back non-zero where the protocol requires all zeroes."""

    return "command 64 status" in message and "!= 0000000000000000" in message


def _journal_error(attempt: Any) -> str | None:
    journal = attempt.journal
    if isinstance(journal, dict):
        error = journal.get("error")
        if isinstance(error, str):
            return error
    return None


def _translate_finalization_error(error: SinglePassWorkflowError) -> PyCoolscanError:
    """Map a completed-frame finalization failure to the public exception
    it corresponds to in the API contract's exception table.

    ``LS5000SinglePassWorkflow.finalize_attempt`` raises one shared
    ``SinglePassIntegrityError``/``SinglePassWorkflowError`` for every
    failure mode (decode-shape mismatch, roll-identity inconsistency, smear
    QC refusal, ...) -- there is no per-cause exception type to catch
    instead. The two causes the API contract names explicitly are
    recognized by message text; everything else falls back to
    ``BatchIntegrityError`` (self-verification failed before publication),
    which is the contract's own catch-all for this layer.
    """

    message = str(error)
    if "stopped-transport smear QC refused" in message:
        verdict = "smear"
        reason = message
        marker = "refused decoded RGB: "
        if marker in message:
            tail = message.split(marker, 1)[1]
            verdict, _, reason = tail.partition(": ")
        return TransportSmearDetected(
            message,
            assessment=TransportSmearAssessment(
                verdict=verdict if verdict in ("smear", "indeterminate") else "indeterminate",
                start_row=None,
                suffix_rows=0,
                minimum_matches=0,
                tail_median_rms=None,
                tail_min_corr=None,
                pre_tail_median_rms=None,
                texture_span=None,
                reason=reason or message,
            ),
        )
    if "decoder returned" in message and "expected" in message:
        return GeometryValidationError(message)
    return BatchIntegrityError(message)


def _array_evidence(array: np.ndarray) -> ArtifactEvidence:
    contiguous = np.ascontiguousarray(array)
    payload = memoryview(contiguous).cast("B")
    return ArtifactEvidence(
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_length=payload.nbytes,
        shape=tuple(contiguous.shape),
        dtype=str(contiguous.dtype),
    )


def _ticks_to_microseconds(raw: object) -> float:
    """Convert one raw 10ns hardware exposure tick count to microseconds."""

    return float(raw) * 0.01 if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0


def _build_receipt(
    manifest: dict[str, Any],
    *,
    device_id: str,
    artifacts: dict[str, ArtifactEvidence],
) -> Receipt:
    """Adapt a completed ``LS5000SinglePassWorkflow`` manifest dict into the
    public ``Receipt`` shape.

    ``exposure.focus_position``/``.exposure_multiplier`` are fixed
    placeholders (0 / 1.0): the single-pass RGBI4 protocol's own telemetry is
    raw per-channel hardware exposure ticks with no focus-position or
    exposure-multiplier concept, unlike the SANE-based plain-scan path's
    ``ScannerCaptureState``. ``red/green/blue_exposure_us`` are populated
    faithfully by converting those raw 10ns ticks to microseconds.
    ``split_alignment`` is always ``None`` for this route: RGB and IR share
    one pass with no separate registration step.
    """

    capture = manifest["capture"]
    frame_evidence = manifest["frame_evidence"]
    exposure_evidence = manifest["exposure_evidence"]
    quality_control = manifest["quality_control"]
    identity = manifest["identity"]

    roll_identity = frame_evidence.get("roll_identity") or {}
    manual_payload = frame_evidence.get("manual_review_approval")
    manual_approval = None
    if manual_payload:
        manual_approval = ApprovalReceipt(
            reviewed_fingerprint_sha256=manual_payload["reviewed_fingerprint_sha256"],
            slot=manual_payload["slot"],
            spacing_offset=manual_payload["boundary_offset_rows"],
            thumbnail_sha256=manual_payload["thumbnail_sha256"],
            reviewed_lookup_row=manual_payload["reviewed_lookup_row"],
            reviewed_native_origin=manual_payload["reviewed_native_origin"],
            review_reasons=tuple(manual_payload["review_reasons"]),
        )

    wire = (exposure_evidence.get("accepted_contract") or {}).get("wire_colors_raw_10ns") or {}
    exposure = ExposureVector(
        focus_position=0,
        exposure_multiplier=1.0,
        red_exposure_us=_ticks_to_microseconds(wire.get("1")),
        green_exposure_us=_ticks_to_microseconds(wire.get("2")),
        blue_exposure_us=_ticks_to_microseconds(wire.get("3")),
    )

    smear = quality_control["stopped_transport_smear"]["assessment"]
    transport_smear = TransportSmearAssessment(
        verdict=smear["verdict"],
        start_row=smear.get("start_row"),
        suffix_rows=smear["suffix_rows"],
        minimum_matches=smear["minimum_matches"],
        tail_median_rms=smear.get("tail_median_rms"),
        tail_min_corr=smear.get("tail_min_corr"),
        pre_tail_median_rms=smear.get("pre_tail_median_rms"),
        texture_span=smear.get("texture_span"),
        reason=smear["reason"],
    )
    clip = quality_control["capture_clipping"]
    clipping = ClippingTelemetry(
        fractions=tuple(clip["fractions"]),
        clip_level=clip["clip_level"],
        warning_fraction=clip["warning_fraction"],
        warning=clip["warning"],
    )
    focus = quality_control["focus_detail"]
    focus_detail = FocusDetailTelemetry(
        method=focus["method"],
        verdict=focus["verdict"],
        score=focus.get("score"),
        texture_span=focus["texture_span"],
    )

    return Receipt(
        version=RECEIPT_VERSION,
        slot=identity["selected_slot"],
        spacing_offset=identity["boundary_offset_rows"],
        dpi=LS5000_FINE_DPI,
        depth=LS5000_FINE_DEPTH,
        device_id=device_id,
        device_model=capture["scanner_identity"],
        reviewed_fingerprint_sha256=roll_identity.get("reviewed_fingerprint_sha256", ""),
        fresh_fingerprint_sha256=roll_identity.get("fresh_fingerprint_sha256", ""),
        manual_approval=manual_approval,
        exposure=exposure,
        split_alignment=None,
        clipping=clipping,
        focus_detail=focus_detail,
        transport_smear=transport_smear,
        artifacts=artifacts,
    )


def _read_frame(
    finalization: SinglePassFinalizationResult,
    *,
    slot: int,
    device_id: str,
) -> Frame:
    output_paths = finalization.output_paths
    rgb = tifffile.imread(output_paths["rgb"])
    ir = tifffile.imread(output_paths["ir"])
    ir_validity = tifffile.imread(output_paths["ir_valid_mask"]).astype(bool)
    artifacts = {"rgb": _array_evidence(rgb), "ir": _array_evidence(ir)}
    receipt = _build_receipt(finalization.manifest, device_id=device_id, artifacts=artifacts)
    _cleanup_finalization(finalization)
    return Frame(slot=slot, rgb=rgb, ir=ir, ir_validity=ir_validity, receipt=receipt)


def _cleanup_finalization(finalization: SinglePassFinalizationResult) -> None:
    """Remove the private scratch/output files this facade wrote so it never
    leaves a public-looking output folder behind (see module §8 in the API
    spec: this package returns arrays, not files)."""

    for path in finalization.output_paths.values():
        path.unlink(missing_ok=True)
    finalization.manifest_path.unlink(missing_ok=True)
    checkpoint = finalization.manifest_path.parent / "scratch-deletion.json"
    checkpoint.unlink(missing_ok=True)
    try:
        finalization.manifest_path.parent.rmdir()
    except OSError:
        pass
