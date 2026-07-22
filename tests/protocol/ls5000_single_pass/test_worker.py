"""Hardware-free contracts for the packaged LS-5000 capture worker."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from coolscanpy.protocol.ls5000_single_pass import worker as worker_module
from coolscanpy.protocol.ls5000_single_pass.capture_process import (
    ManualFrameApproval,
    ReviewedRollFingerprint,
    build_reviewed_roll_fingerprint,
)
from coolscanpy.protocol.ls5000_single_pass.roll_index import (
    NativeFrameOrigin,
    TransportMapping,
)
from coolscanpy.protocol.ls5000_single_pass.plan import (
    CANONICAL_PLAN_SHA256,
    load_canonical_plan,
)
from coolscanpy.protocol.ls5000_single_pass.continuation_plan import (
    load_canonical_continuation_plan,
)
from coolscanpy.protocol.ls5000_single_pass.worker import (
    DATA_PACKAGE,
    FRAME_TABLE_SEND_BYTES,
    FRAME_TABLE_SEND_RECORDS,
    ProtocolError,
    TransactionResult,
    _cleanup_synchronized,
    _bind_plan_to_live_selection,
    _derive_index_geometry,
    compile_continuation_steps,
    load_validated_batch_job,
    main,
    wait_for_parent_ack,
    _meter_controller_sha256,
    apply_boundary_offset,
    apply_batch_boundary_offsets,
    build_live_frame_table_payload,
)
from importlib.resources import files
from coolscanpy.protocol.ls5000_single_pass.roll_index import TransportRecord
from coolscanpy.protocol.ls5000_single_pass.window import decode_window_block


LIVE8_TRANSPORT_FIELDS = (
    (6202, 8, 22),
    (12250, 16, 22),
    (18326, 24, 26),
    (24360, 32, 24),
    (30436, 40, 28),
    (36484, 48, 28),
    (42532, 56, 28),
    (48608, 64, 32),
    (54614, 72, 26),
    (60676, 80, 28),
    (66710, 88, 26),
    (72758, 96, 26),
    (78778, 104, 22),
    (84896, 112, 32),
    (90916, 120, 28),
    (96992, 128, 32),
    (103026, 136, 30),
    (109046, 144, 26),
    (115136, 152, 32),
    (121156, 160, 28),
    (127176, 168, 24),
    (133280, 176, 32),
    (139286, 184, 26),
    (145362, 192, 30),
    (151396, 200, 28),
    (157416, 208, 24),
    (163464, 216, 24),
    (169526, 224, 26),
    (175560, 232, 24),
    (181636, 240, 28),
    (187684, 248, 28),
    (193746, 256, 30),
    (199794, 264, 30),
    (205842, 272, 30),
    (211904, 280, 32),
    (217924, 288, 28),
    (223958, 296, 260),
)


def _reviewed_fingerprint() -> ReviewedRollFingerprint:
    return ReviewedRollFingerprint(
        source_preview_sha256="1" * 64,
        source_table_sha256="2" * 64,
        preview_shape=(6_104, 96, 3),
        frame_start_rows=tuple(100 + 143 * index for index in range(40)),
        frame_native_origins=tuple(6_000 + 6_000 * index for index in range(40)),
        frame_visual_hashes=tuple(f"{index:064x}" for index in range(40)),
        frame_visual_log_spans=(2.0,) * 40,
    )


def _density_calibration(session_id: str) -> worker_module.DensityCalibration:
    reads = [
        worker_module.decode_density_calibration_read(
            bytes.fromhex(f"28008c000{color}0300000a80"),
            bytes.fromhex(payload),
        )
        for color, payload in enumerate(
            (
                "8c20000000040000df1a",
                "8c20000000040000bba4",
                "8c200000000400007fab",
            ),
            start=1,
        )
    ]
    return worker_module.assemble_density_calibration(
        reads,
        session_id=session_id,
    )


def _reviewed_fingerprint_with_count(count: int) -> ReviewedRollFingerprint:
    """A reviewed fingerprint describing exactly ``count`` signed slots."""

    return ReviewedRollFingerprint(
        source_preview_sha256="1" * 64,
        source_table_sha256="2" * 64,
        preview_shape=(6_104, 96, 3),
        frame_start_rows=tuple(100 + 143 * index for index in range(count)),
        frame_native_origins=tuple(6_000 + 6_000 * index for index in range(count)),
        frame_visual_hashes=tuple(f"{index:064x}" for index in range(count)),
        frame_visual_log_spans=(2.0,) * count,
    )


def test_frozen_worker_uses_pinned_meter_identity_without_loose_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_source = Path("/frozen/NegPy.app/Contents/Resources/meter.py")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "coolscanpy.protocol.ls5000_single_pass.worker.meter_module.__file__",
        str(missing_source),
    )


def test_usb_device_selection_requires_exact_reviewed_sane_topology() -> None:
    wrong = SimpleNamespace(bus=1, address=9)
    exact = SimpleNamespace(bus=1, address=2)
    calls: list[dict[str, object]] = []

    def find(**kwargs: object) -> tuple[object, ...]:
        calls.append(kwargs)
        return wrong, exact

    selected = worker_module._find_ls5000_usb_device(
        SimpleNamespace(find=find),
        expected_bus=1,
        expected_address=2,
    )

    assert selected is exact
    assert calls == [
        {
            "idVendor": 0x04B0,
            "idProduct": 0x4002,
            "find_all": True,
            "backend": None,
        }
    ]


def test_usb_device_selection_refuses_missing_or_ambiguous_exact_topology() -> None:
    for devices in (
        (SimpleNamespace(bus=1, address=9),),
        (
            SimpleNamespace(bus=1, address=2),
            SimpleNamespace(bus=1, address=2),
        ),
    ):
        core = SimpleNamespace(find=lambda **_kwargs: devices)
        with pytest.raises(ProtocolError, match="exact USB topology"):
            worker_module._find_ls5000_usb_device(
                core,
                expected_bus=1,
                expected_address=2,
            )


@pytest.mark.parametrize("meter_only", (False, True))
def test_live_full_and_meter_refuse_usb_first_device_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meter_only: bool,
) -> None:
    connect_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        lambda **kwargs: connect_calls.append(kwargs),
    )

    with pytest.raises(ProtocolError, match="require exact USB bus and address"):
        worker_module.run_live_capture(
            load_canonical_plan(),
            tmp_path / "plan.jsonl",
            CANONICAL_PLAN_SHA256,
            tmp_path / "capture.bin",
            tmp_path / "journal.json",
            worker_module.EXPECTED_FINE_READS,
            frame=17,
            meter_only=meter_only,
        )

    assert connect_calls == []


def test_live_child_rejects_altered_self_consistent_plan_before_usb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.jsonl"
    manifest_path = tmp_path / "manifest.json"
    plan = load_canonical_plan()
    plan[1] = {**plan[1], "cdb": "1b0000000000"}
    plan_payload = b"".join(
        (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for entry in plan
    )
    plan_path.write_bytes(plan_payload)
    manifest = json.loads(
        (worker_module.HERE / "data" / "replay-first-rgbi4-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest["plan_sha256"] = hashlib.sha256(plan_payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    connect_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        lambda **kwargs: connect_calls.append(kwargs),
    )

    with pytest.raises(ProtocolError, match="packaged canonical plan"):
        main(
            [
                "--plan",
                str(plan_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(tmp_path / "capture.bin"),
                "--journal",
                str(tmp_path / "journal.json"),
                "--preview-only",
                "--expected-usb-bus",
                "1",
                "--expected-usb-address",
                "2",
                "--live",
            ]
        )

    assert connect_calls == []


def test_live_child_revalidates_parent_bundle_identity_before_usb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = worker_module.HERE / "data" / "replay-first-rgbi4-plan.jsonl"
    manifest_path = worker_module.HERE / "data" / "replay-first-rgbi4-manifest.json"
    connect_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        worker_module, "verify_capture_bundle", lambda **_kwargs: "0" * 64
    )
    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        lambda **kwargs: connect_calls.append(kwargs),
    )

    with pytest.raises(ProtocolError, match="changed after parent verification"):
        main(
            [
                "--plan",
                str(plan_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(tmp_path / "capture.bin"),
                "--journal",
                str(tmp_path / "journal.json"),
                "--preview-only",
                "--expected-usb-bus",
                "1",
                "--expected-usb-address",
                "2",
                "--expected-capture-bundle-sha256",
                worker_module.CAPTURE_BUNDLE_SHA256,
                "--live",
            ]
        )

    assert connect_calls == []


def test_continuation_compiler_emits_the_pinned_89_steps_without_session_commands() -> (
    None
):
    steps = compile_continuation_steps(
        load_canonical_plan(),
        load_canonical_continuation_plan(),
    )

    assert len(steps) == 89
    assert [step.code for step in steps] == [
        raw[0]
        for raw in load_canonical_continuation_plan()["trace_equivalence"][
            "semantic_steps"
        ]
    ]
    sequences = [entry["seq"] for step in steps for entry in step.entries]
    assert sequences[0] == 225
    assert sequences[-1] == 606
    assert 500 not in sequences
    assert not {
        "RESERVE_UNIT",
        "RELEASE_UNIT",
        "VENDOR_E0:EJECT",
        "SEND:sub_008f",
    }.intersection(entry["name"] for step in steps for entry in step.entries)
    assert [step.code for step in steps].count("R") == 15


def test_batch_job_loader_binds_ordered_frame_paths_and_parent_ack_contract(
    tmp_path: Path,
) -> None:
    session_id = "batch-slot17-slot19-session"
    fingerprint = _reviewed_fingerprint()
    approval = ManualFrameApproval(
        reviewed_fingerprint_sha256=fingerprint.binding_sha256,
        slot=17,
        boundary_offset_rows=-12,
        thumbnail_sha256="3" * 64,
        reviewed_lookup_row=2_400,
        reviewed_native_origin=100_000,
        review_reasons=("transport-origin-inferred",),
    )
    job_path = tmp_path / "batch-job.json"
    job_path.write_text(
        json.dumps(
            {
                "apply_all_boundary_offsets_before_first_frame": True,
                "capture_plan_sha256": "a" * 64,
                "continuation_plan_sha256": "b" * 64,
                "expected_usb_address": 2,
                "expected_usb_bus": 1,
                "frames": [
                    {
                        "ack": "frame-017/parent-ack.json",
                        "boundary_offset_rows": -12,
                        "journal": "frame-017/journal.json",
                        "manual_review_approval": approval.to_payload(),
                        "output": "frame-017/capture.bin",
                        "slot": 17,
                    },
                    {
                        "ack": "frame-019/parent-ack.json",
                        "boundary_offset_rows": 8,
                        "journal": "frame-019/journal.json",
                        "manual_review_approval": None,
                        "output": "frame-019/capture.bin",
                        "slot": 19,
                    },
                ],
                "parent_ack_required_after_every_frame": True,
                "release_once_after_last_frame": True,
                "reviewed_roll_fingerprint": fingerprint.to_payload(),
                "schema_version": 3,
                "session_contract": "one-process-one-reservation",
                "session_id": session_id,
            }
        ),
        encoding="utf-8",
    )

    job = load_validated_batch_job(
        job_path,
        expected_job_sha256=hashlib.sha256(job_path.read_bytes()).hexdigest(),
        expected_plan_sha256="a" * 64,
        expected_continuation_sha256="b" * 64,
    )

    assert job.session_id == session_id
    assert job.selected_slots == (17, 19)
    assert job.reviewed_fingerprint == fingerprint
    assert (job.expected_usb_bus, job.expected_usb_address) == (1, 2)
    assert job.frames[0].manual_review_approval == approval
    assert job.frames[1].manual_review_approval is None
    assert [frame.boundary_offset_rows for frame in job.frames] == [-12, 8]
    assert job.job_sha256 == hashlib.sha256(job_path.read_bytes()).hexdigest()
    assert job.frames[0].output == tmp_path / "frame-017" / "capture.bin"
    assert job.frames[0].journal == tmp_path / "frame-017" / "journal.json"
    assert job.frames[0].ack == tmp_path / "frame-017" / "parent-ack.json"


def test_parent_ack_is_bound_to_session_frame_slot_and_fresh_nonce(
    tmp_path: Path,
) -> None:
    ack_path = tmp_path / "parent-ack.json"
    ack_path.write_text(
        json.dumps(
            {
                "ack_nonce": "fresh-nonce-17",
                "action": "continue",
                "frame_index": 1,
                "schema_version": 1,
                "session_id": "batch-session",
                "slot": 17,
            }
        ),
        encoding="utf-8",
    )

    action = wait_for_parent_ack(
        ack_path,
        session_id="batch-session",
        frame_index=1,
        slot=17,
        nonce="fresh-nonce-17",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert action == "continue"

    assert _meter_controller_sha256() == (
        "c7d00c9c8796b7264a553848106a1fe075ab4a25315fbe5a05d05bc35515ca10"
    )


def test_batch_cli_dry_run_validates_one_session_without_single_frame_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = files(DATA_PACKAGE)
    plan_path = Path(data.joinpath("replay-first-rgbi4-plan.jsonl"))
    manifest_path = Path(data.joinpath("replay-first-rgbi4-manifest.json"))
    continuation_path = Path(data.joinpath("replay-next-rgbi4-plan.json"))
    fingerprint = _reviewed_fingerprint()
    job_path = tmp_path / "batch-job.json"
    job_path.write_text(
        json.dumps(
            {
                "apply_all_boundary_offsets_before_first_frame": True,
                "capture_plan_sha256": hashlib.sha256(
                    plan_path.read_bytes()
                ).hexdigest(),
                "continuation_plan_sha256": hashlib.sha256(
                    continuation_path.read_bytes()
                ).hexdigest(),
                "expected_usb_address": 2,
                "expected_usb_bus": 1,
                "frames": [
                    {
                        "ack": "frame-017/parent-ack.json",
                        "boundary_offset_rows": -12,
                        "journal": "frame-017/journal.json",
                        "manual_review_approval": None,
                        "output": "frame-017/capture.bin",
                        "slot": 17,
                    },
                    {
                        "ack": "frame-019/parent-ack.json",
                        "boundary_offset_rows": 8,
                        "journal": "frame-019/journal.json",
                        "manual_review_approval": None,
                        "output": "frame-019/capture.bin",
                        "slot": 19,
                    },
                ],
                "parent_ack_required_after_every_frame": True,
                "release_once_after_last_frame": True,
                "reviewed_roll_fingerprint": fingerprint.to_payload(),
                "schema_version": 3,
                "session_contract": "one-process-one-reservation",
                "session_id": "batch-dry-run",
            }
        ),
        encoding="utf-8",
    )

    main(
        [
            "--batch-job",
            str(job_path),
            "--expected-batch-job-sha256",
            hashlib.sha256(job_path.read_bytes()).hexdigest(),
            "--plan",
            str(plan_path),
            "--continuation-plan",
            str(continuation_path),
            "--manifest",
            str(manifest_path),
            "--session-journal",
            str(tmp_path / "session-journal.json"),
        ]
    )

    output = capsys.readouterr().out
    assert "validated RGBI4x batch" in output
    assert "slots 17, 19" in output
    assert "dry run only; scanner was not accessed" in output
    assert not (tmp_path / "frame-017").exists()


def test_batch_cli_refuses_a_topology_rewrite_before_any_usb_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = files(DATA_PACKAGE)
    plan_path = Path(data.joinpath("replay-first-rgbi4-plan.jsonl"))
    manifest_path = Path(data.joinpath("replay-first-rgbi4-manifest.json"))
    continuation_path = Path(data.joinpath("replay-next-rgbi4-plan.json"))
    fingerprint = _reviewed_fingerprint()
    job_path = tmp_path / "batch-job.json"
    job = {
        "apply_all_boundary_offsets_before_first_frame": True,
        "capture_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "continuation_plan_sha256": hashlib.sha256(
            continuation_path.read_bytes()
        ).hexdigest(),
        "expected_usb_address": 2,
        "expected_usb_bus": 1,
        "frames": [
            {
                "ack": "frame-017/parent-ack.json",
                "boundary_offset_rows": 0,
                "journal": "frame-017/journal.json",
                "manual_review_approval": None,
                "output": "frame-017/capture.bin",
                "slot": 17,
            }
        ],
        "parent_ack_required_after_every_frame": True,
        "release_once_after_last_frame": True,
        "reviewed_roll_fingerprint": fingerprint.to_payload(),
        "schema_version": 3,
        "session_contract": "one-process-one-reservation",
        "session_id": "batch-tampered-topology",
    }
    job_path.write_text(json.dumps(job), encoding="utf-8")
    prepared_sha256 = hashlib.sha256(job_path.read_bytes()).hexdigest()

    job["expected_usb_address"] = 9
    job_path.write_text(json.dumps(job), encoding="utf-8")
    connect_calls: list[object] = []
    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        lambda **kwargs: connect_calls.append(kwargs),
    )

    with pytest.raises(ProtocolError, match="SHA-256 mismatch before USB access"):
        main(
            [
                "--batch-job",
                str(job_path),
                "--expected-batch-job-sha256",
                prepared_sha256,
                "--plan",
                str(plan_path),
                "--continuation-plan",
                str(continuation_path),
                "--manifest",
                str(manifest_path),
                "--session-journal",
                str(tmp_path / "session-journal.json"),
                "--live",
            ]
        )

    assert connect_calls == []
    assert not (tmp_path / "session-journal.json").exists()


def test_synchronized_cleanup_receipt_records_the_single_release_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases: list[tuple[object, object]] = []

    def release(ep_out: object, ep_in: object) -> TransactionResult:
        releases.append((ep_out, ep_in))
        return TransactionResult(1, b"", bytes.fromhex("0000000000000000"), "000000", 2)

    monkeypatch.setattr(
        "coolscanpy.protocol.ls5000_single_pass.worker._release_unit",
        release,
    )

    receipt = _cleanup_synchronized(
        "out",
        "in",
        scan_active=False,
        ready_required=False,
        reserved=True,
    )

    assert releases == [("out", "in")]
    assert receipt == {
        "attempted": True,
        "complete": True,
        "release_attempted": True,
        "release_status": "0000000000000000",
        "release_succeeded": True,
    }


def test_live_batch_connect_failure_records_no_reservation_and_no_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "batch-connect-failure"
    root.mkdir()
    frame = worker_module.BatchFrameSpec(
        slot=17,
        boundary_offset_rows=0,
        output=root / "frame-017" / "capture.bin",
        journal=root / "frame-017" / "journal.json",
        ack=root / "frame-017" / "parent-ack.json",
    )
    frame.output.parent.mkdir()
    batch = worker_module.LiveBatchJob(
        session_id="batch-connect-failure",
        root=root,
        frames=(frame,),
        reviewed_fingerprint=_reviewed_fingerprint(),
        expected_usb_bus=1,
        expected_usb_address=2,
        plan_sha256=CANONICAL_PLAN_SHA256,
        continuation_plan_sha256=(worker_module.CANONICAL_CONTINUATION_PLAN_SHA256),
        job_sha256="c" * 64,
    )
    session_journal = root / "session-journal.json"
    connect_calls: list[dict[str, int | None]] = []

    def fail_connect(**kwargs: int | None) -> object:
        connect_calls.append(kwargs)
        raise ProtocolError("USB device absent")

    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        fail_connect,
    )

    with pytest.raises(ProtocolError, match="USB device absent"):
        worker_module.run_live_capture(
            load_canonical_plan(),
            tmp_path / "plan.jsonl",
            CANONICAL_PLAN_SHA256,
            frame.output,
            frame.journal,
            worker_module.EXPECTED_FINE_READS,
            frame=17,
            boundary_offset_rows=0,
            batch_job=batch,
            continuation_plan=load_canonical_continuation_plan(),
            continuation_plan_sha256=(worker_module.CANONICAL_CONTINUATION_PLAN_SHA256),
            session_journal_path=session_journal,
        )

    receipt = json.loads(session_journal.read_text(encoding="utf-8"))
    assert connect_calls == [{"expected_usb_bus": 1, "expected_usb_address": 2}]
    assert receipt["status"] == "failed"
    assert receipt["reservation_acquired"] is False
    assert receipt["unit_release_attempts"] == 0
    assert receipt["unit_released"] is False
    assert receipt["recovery_required"] == "none"


def test_live_batch_refuses_a_connected_scanner_that_changed_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "batch-topology-mismatch"
    root.mkdir()
    frame = worker_module.BatchFrameSpec(
        slot=17,
        boundary_offset_rows=0,
        output=root / "frame-017" / "capture.bin",
        journal=root / "frame-017" / "journal.json",
        ack=root / "frame-017" / "parent-ack.json",
    )
    frame.output.parent.mkdir()
    batch = worker_module.LiveBatchJob(
        session_id="batch-topology-mismatch",
        root=root,
        frames=(frame,),
        reviewed_fingerprint=_reviewed_fingerprint(),
        expected_usb_bus=1,
        expected_usb_address=2,
        plan_sha256=CANONICAL_PLAN_SHA256,
        continuation_plan_sha256=(worker_module.CANONICAL_CONTINUATION_PLAN_SHA256),
        job_sha256="c" * 64,
    )

    class USBUtil:
        @staticmethod
        def release_interface(_device: object, _interface_number: int) -> None:
            return None

        @staticmethod
        def dispose_resources(_device: object) -> None:
            return None

    connect_calls: list[dict[str, int | None]] = []

    def connect(**kwargs: int | None) -> tuple[object, object, object, object, object]:
        connect_calls.append(kwargs)
        return (
            SimpleNamespace(bus=1, address=9),
            SimpleNamespace(bInterfaceNumber=0),
            object(),
            object(),
            USBUtil,
        )

    monkeypatch.setattr(worker_module, "_connect_device", connect)
    session_journal = root / "session-journal.json"

    with pytest.raises(ProtocolError, match="exact requested USB topology"):
        worker_module.run_live_capture(
            load_canonical_plan(),
            tmp_path / "plan.jsonl",
            CANONICAL_PLAN_SHA256,
            frame.output,
            frame.journal,
            worker_module.EXPECTED_FINE_READS,
            frame=17,
            boundary_offset_rows=0,
            batch_job=batch,
            continuation_plan=load_canonical_continuation_plan(),
            continuation_plan_sha256=(worker_module.CANONICAL_CONTINUATION_PLAN_SHA256),
            session_journal_path=session_journal,
        )

    assert connect_calls == [{"expected_usb_bus": 1, "expected_usb_address": 2}]
    receipt = json.loads(session_journal.read_text(encoding="utf-8"))
    assert receipt["expected_usb_bus"] == 1
    assert receipt["expected_usb_address"] == 2
    assert receipt["actual_usb_bus"] is None
    assert receipt["actual_usb_address"] is None
    assert receipt["reservation_acquired"] is False
    assert receipt["recovery_required"] == "none"


def _mapping(fields: tuple[tuple[int, int, int], ...]) -> TransportMapping:
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=frame * 144,
            lookup_row=frame * 144,
            code=code,
            selector=selector,
            native_origin=native_origin,
            method="test-fixture",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, (native_origin, selector, code) in enumerate(fields, start=1)
    )
    return TransportMapping(
        record_count=len(origins),
        native_intercept=0.0,
        native_units_per_preview_row=42.0,
        anchor_mae_rows=0.0,
        anchor_max_error_rows=0.0,
        origins=origins,
    )


def _short_strip_mapping(
    count: int,
    *,
    non_addressable_trailing: int = 0,
) -> tuple[TransportMapping, tuple[TransportRecord, ...]]:
    """A live-shaped mapping/records pair shorter than a full roll.

    ``non_addressable_trailing`` marks that many of the last ``count``
    origins as outside the index raster, the same way a real detector would
    flag a candidate slot build_live_frame_table_payload must not address.
    """

    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    lookup_rows = tuple(100 + 143 * index for index in range(count))
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(
                ("outside-index-raster",)
                if frame > count - non_addressable_trailing
                else ()
            ),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(lookup_rows, start=1)
    )
    return TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins), records


def test_live8_frame_table_is_the_exact_firmware_accepted_payload() -> None:
    payload = build_live_frame_table_payload(_mapping(LIVE8_TRANSPORT_FIELDS))
    send = next(entry for entry in load_canonical_plan() if entry["seq"] == 174)

    assert FRAME_TABLE_SEND_RECORDS == 37
    assert FRAME_TABLE_SEND_BYTES == 300
    assert send["cdb"] == "2a008f00000300012c00"
    assert len(payload) == FRAME_TABLE_SEND_BYTES
    assert payload[:4] == bytes.fromhex("012a2500")
    assert (
        hashlib.sha256(payload).hexdigest()
        == "b78f6d8a1df1e0d5b242eda27eca88d121a6db2d2e64cf55ae9305142e39fc08"
    )


def test_frame_table_refuses_fewer_than_two_origins() -> None:
    with pytest.raises(ProtocolError, match="fewer than 2"):
        build_live_frame_table_payload(_mapping(LIVE8_TRANSPORT_FIELDS[:1]))


def test_frame_table_accepts_a_short_strip_mapping_below_37_origins() -> None:
    payload = build_live_frame_table_payload(_mapping(LIVE8_TRANSPORT_FIELDS[:6]))

    assert payload[2] == 6
    assert len(payload) == 4 + 6 * 8


def test_frame_table_ignores_advisory_slots_after_the_fixed_37_records() -> None:
    extra = (230000, 304, 24)
    payload = build_live_frame_table_payload(_mapping((*LIVE8_TRANSPORT_FIELDS, extra)))

    assert len(payload) == FRAME_TABLE_SEND_BYTES
    assert (
        hashlib.sha256(payload).hexdigest()
        == "b78f6d8a1df1e0d5b242eda27eca88d121a6db2d2e64cf55ae9305142e39fc08"
    )


def test_boundary_offset_resolves_raw_identity_from_the_same_transport_table() -> None:
    mapping = _mapping(LIVE8_TRANSPORT_FIELDS)
    records = tuple(
        TransportRecord(
            row=row,
            code=22 + 2 * (row % 4),
            selector=row,
            native_origin=756 * row + 7 * (22 + 2 * (row % 4)),
        )
        for row in range(6_000)
    )

    adjusted, selected = apply_boundary_offset(
        mapping,
        records,
        frame=18,
        offset_rows=-73,
    )

    assert selected.lookup_row == mapping.origins[17].lookup_row - 73
    source_record = records[selected.lookup_row]
    assert (selected.code, selected.selector, selected.native_origin) == (
        source_record.code,
        source_record.selector,
        source_record.native_origin,
    )
    assert adjusted.origins[17] is selected
    assert selected.automatic is True
    assert adjusted.origins[:17] == mapping.origins[:17]
    assert adjusted.origins[18:] == mapping.origins[18:]


def test_internal_window_decoder_reads_the_fields_the_worker_patches() -> None:
    payload = bytearray(58)
    payload[7] = 50
    payload[8] = 9
    payload[10:14] = bytes.fromhex("0fa00fa0")
    payload[14:18] = (12).to_bytes(4, "big")
    payload[18:22] = (109_060).to_bytes(4, "big")
    payload[22:26] = (3_946).to_bytes(4, "big")
    payload[26:30] = (5_959).to_bytes(4, "big")
    payload[34] = 16
    payload[48] = 0x30
    payload[50] = 0x01
    payload[51] = 0x10
    payload[54:58] = (120_000).to_bytes(4, "big")

    decoded = decode_window_block(payload)

    assert decoded is not None
    assert decoded["color_name"] == "IR"
    assert decoded["upper_left_y"] == 109_060
    assert decoded["width"] == 3_946
    assert decoded["height"] == 5_959
    assert decoded["samples_per_scan_minus1_nibble"] == 3
    assert decoded["is_multi_sample"] is True
    assert decoded["exposure_raw_10ns"] == 120_000
    assert decode_window_block(payload[:-1]) is None


def test_resolved_offset_is_encoded_into_the_selected_fixed_table_record() -> None:
    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    lookup_rows = tuple(100 + 143 * index for index in range(37))
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(lookup_rows, start=1)
    )
    mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)

    adjusted, selected = apply_boundary_offset(
        mapping,
        records,
        frame=18,
        offset_rows=10,
    )
    payload = build_live_frame_table_payload(adjusted)

    encoded = struct.unpack_from(">IHH", payload, 4 + 17 * 8)
    assert encoded == (selected.native_origin, selected.selector, selected.code)
    assert selected.lookup_row == lookup_rows[17] + 10


def test_batch_offsets_share_the_one_retained_table_and_later_frame_origins() -> None:
    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    lookup_rows = tuple(100 + 143 * index for index in range(37))
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(lookup_rows, start=1)
    )
    mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)

    combined, resolved = apply_batch_boundary_offsets(
        mapping,
        records,
        ((7, 9), (18, -11), (23, 6)),
    )

    assert [selected.lookup_row for _base, selected in resolved] == [
        lookup_rows[6] + 9,
        lookup_rows[17] - 11,
        lookup_rows[22] + 6,
    ]
    plan = load_canonical_plan()
    geometry = _derive_index_geometry(plan)
    bindings = []
    for slot in (7, 18, 23):
        selection = SimpleNamespace(
            frame=slot,
            frame_count=len(combined.origins),
            geometry=geometry,
            mapping=combined,
            selected=combined.origins[slot - 1],
        )
        bindings.append(_bind_plan_to_live_selection(plan, selection))

    # The first and only transmitted frame table already contains every
    # selected frame's operator-adjusted raw transport identity.
    retained_table = bytes.fromhex(bindings[0][173]["data_out"])
    for slot in (7, 18, 23):
        selected = combined.origins[slot - 1]
        assert struct.unpack_from(">IHH", retained_table, 4 + (slot - 1) * 8) == (
            selected.native_origin,
            selected.selector,
            selected.code,
        )

    # Every later continuation binds autofocus and all RGBI windows to the
    # exact same origin already stored in that retained table.
    for slot, bound in zip((7, 18, 23), bindings, strict=True):
        origin = combined.origins[slot - 1].native_origin
        autofocus = bytes.fromhex(bound[230]["data_out"])
        assert int.from_bytes(autofocus[5:9], "big") == origin + 2_979
        for sequence in (
            503,
            504,
            505,
            506,
            530,
            531,
            532,
            533,
            556,
            557,
            558,
            559,
            581,
            582,
            583,
            584,
        ):
            window = decode_window_block(bytes.fromhex(bound[sequence - 1]["data_out"]))
            assert window is not None
            assert window["upper_left_y"] == origin


def test_inferred_batch_origin_requires_its_receipt_bound_operator_approval() -> None:
    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    lookup_rows = tuple(100 + 143 * index for index in range(37))
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method=(
                "affine-guided-local-lookup"
                if frame == 18
                else "direct-gap-trailing-row"
            ),
            automatic=frame != 18,
            manual_review=frame == 18,
            review_reasons=(("transport-origin-inferred",) if frame == 18 else ()),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(lookup_rows, start=1)
    )
    mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)

    with pytest.raises(ProtocolError, match="requires manual review"):
        apply_batch_boundary_offsets(mapping, records, ((18, -11),))

    combined, resolved = apply_batch_boundary_offsets(
        mapping,
        records,
        ((18, -11),),
        approved_manual_slots=frozenset({18}),
    )

    assert resolved[0][0] is origins[17]
    assert resolved[0][1].lookup_row == origins[17].lookup_row - 11
    assert combined.origins[17] is resolved[0][1]


def test_batch_offsets_accept_every_requested_slot_in_a_short_strip_mapping() -> None:
    mapping, records = _short_strip_mapping(6)

    combined, resolved = apply_batch_boundary_offsets(
        mapping, records, ((1, 0), (6, 0))
    )

    assert len(combined.origins) == 6
    assert [selected.frame for _base, selected in resolved] == [1, 6]

    bound_plan = load_canonical_plan()
    geometry = _derive_index_geometry(bound_plan)
    for slot in (1, 6):
        selection = SimpleNamespace(
            frame=slot,
            frame_count=len(combined.origins),
            geometry=geometry,
            mapping=combined,
            selected=combined.origins[slot - 1],
        )
        _bind_plan_to_live_selection(bound_plan, selection)


def test_batch_offsets_refuse_a_requested_slot_beyond_the_addressable_short_table() -> (
    None
):
    # 7 candidate origins, but the 7th lies outside the index raster the same
    # way a real detector would flag an inflated preview candidate -- so the
    # scanner-addressable table this mapping can produce is only 1..6, even
    # though frame 7 exists structurally in `mapping.origins`.
    mapping, records = _short_strip_mapping(7, non_addressable_trailing=1)

    with pytest.raises(
        ProtocolError,
        match=r"requested frame 7 is outside the scanner-addressable table 1\.\.6",
    ):
        apply_batch_boundary_offsets(mapping, records, ((7, 0),))


def test_fresh_batch_index_refuses_a_different_roll_before_plan_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_height = 20
    intervals = tuple(
        (slot * frame_height, (slot + 1) * frame_height) for slot in range(37)
    )
    reviewed_frames = []
    for slot in range(37):
        rng = np.random.default_rng(20_000 + slot)
        reviewed_frames.append(
            np.repeat(
                np.repeat(
                    rng.integers(2_000, 50_000, size=(10, 10, 3), dtype=np.uint16),
                    2,
                    axis=0,
                ),
                2,
                axis=1,
            )
        )
    reviewed_rgb = np.concatenate(reviewed_frames, axis=0)
    fresh_rgb = np.concatenate(list(reversed(reviewed_frames)), axis=0)
    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    lookup_rows = tuple(100 + 143 * index for index in range(37))
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=intervals[frame - 1][0],
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(lookup_rows, start=1)
    )
    mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)
    reviewed = build_reviewed_roll_fingerprint(
        reviewed_rgb,
        frame_intervals=intervals,
        frame_native_origins=tuple(origin.native_origin for origin in origins),
        source_preview_sha256="1" * 64,
        source_table_sha256="2" * 64,
    )
    geometry = SimpleNamespace(
        requested_resolution=97,
        native_resolution=4_000,
        pitch=41,
        native_width=3_946,
        native_height=250_278,
        width=20,
        height=len(fresh_rgb),
        block_bytes=1,
        expected_stream_bytes=1,
    )
    detection = SimpleNamespace(
        confidence="high",
        intervals=tuple(
            SimpleNamespace(start_row=start, end_row=end) for start, end in intervals
        ),
        boundaries=(),
        diagnostics=lambda: {},
    )
    frame_root = tmp_path / "batch"
    frame = worker_module.BatchFrameSpec(
        slot=17,
        boundary_offset_rows=0,
        manual_review_approval=None,
        output=frame_root / "frame-017" / "capture.bin",
        journal=frame_root / "frame-017" / "journal.json",
        ack=frame_root / "frame-017" / "parent-ack.json",
    )
    monkeypatch.setattr(worker_module, "_derive_index_geometry", lambda _plan: geometry)
    monkeypatch.setattr(
        worker_module,
        "validate_live_0x8e_bytes",
        lambda table, _height: (table, len(fresh_rgb)),
    )
    monkeypatch.setattr(
        worker_module,
        "decode_full_index_bytes",
        lambda *_args, **_kwargs: (
            fresh_rgb,
            np.ones(len(fresh_rgb), dtype=bool),
            {},
        ),
    )
    monkeypatch.setattr(
        worker_module, "detect_roll_frames", lambda *_args, **_kwargs: detection
    )
    monkeypatch.setattr(
        worker_module,
        "parse_live_transport_records_bytes",
        lambda *_args, **_kwargs: records,
    )
    monkeypatch.setattr(
        worker_module, "derive_transport_mapping", lambda *_args, **_kwargs: mapping
    )

    with pytest.raises(ProtocolError, match="reviewed roll fingerprint"):
        worker_module._derive_live_batch_selections(
            [],
            b"fresh-preview",
            b"fresh-table",
            (frame,),
            reviewed_fingerprint=reviewed,
        )


def test_batch_selected_slot_gate_runs_for_every_slot_before_plan_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _reviewed_fingerprint()
    fresh = _reviewed_fingerprint()
    context = SimpleNamespace(
        fresh_fingerprint=fresh,
        fingerprint_comparison=worker_module.compare_reviewed_roll_fingerprints(
            reviewed,
            fresh,
        ),
    )
    frame_root = tmp_path / "selected-slot-gate"
    frames = tuple(
        worker_module.BatchFrameSpec(
            slot=slot,
            boundary_offset_rows=0,
            manual_review_approval=None,
            output=frame_root / f"frame-{slot:03d}" / "capture.bin",
            journal=frame_root / f"frame-{slot:03d}" / "journal.json",
            ack=frame_root / f"frame-{slot:03d}" / "parent-ack.json",
        )
        for slot in (7, 17)
    )
    checked: list[int] = []
    plan_bound = False

    def compare_selected(
        _reviewed: ReviewedRollFingerprint,
        _fresh: ReviewedRollFingerprint,
        *,
        slot: int,
    ) -> SimpleNamespace:
        checked.append(slot)
        return SimpleNamespace(
            matches=slot != 17,
            reason=("matched" if slot != 17 else "selected-visual-content-mismatch"),
        )

    def bind_plan(*_args: object, **_kwargs: object) -> None:
        nonlocal plan_bound
        plan_bound = True

    monkeypatch.setattr(
        worker_module,
        "_derive_live_frame_selection",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(
        worker_module,
        "compare_selected_roll_fingerprint",
        compare_selected,
        raising=False,
    )
    monkeypatch.setattr(worker_module, "_bind_plan_to_live_selection", bind_plan)

    with pytest.raises(
        ProtocolError,
        match="selected frame 17.*selected-visual-content-mismatch",
    ):
        worker_module._derive_live_batch_selections(
            [],
            b"fresh-preview",
            b"fresh-table",
            frames,
            reviewed_fingerprint=reviewed,
        )

    assert checked == [7, 17]
    assert plan_bound is False


def _batch_selection_context(
    mapping: TransportMapping,
    records: tuple[TransportRecord, ...],
    reviewed: ReviewedRollFingerprint,
) -> SimpleNamespace:
    fresh = reviewed
    return SimpleNamespace(
        mapping=mapping,
        geometry=SimpleNamespace(height=len(records), native_height=1_000_000),
        usable_rows=0,
        detection=None,
        preview_sha256="a" * 64,
        table_sha256="b" * 64,
        decode_report={},
        reviewed_fingerprint_sha256=reviewed.binding_sha256,
        fresh_fingerprint=fresh,
        fingerprint_comparison=worker_module.compare_reviewed_roll_fingerprints(
            reviewed, fresh
        ),
    )


def _one_slot_batch(
    tmp_path: Path, root_name: str, slot: int
) -> tuple[worker_module.BatchFrameSpec, ...]:
    frame_root = tmp_path / root_name
    return (
        worker_module.BatchFrameSpec(
            slot=slot,
            boundary_offset_rows=0,
            manual_review_approval=None,
            output=frame_root / f"frame-{slot:03d}" / "capture.bin",
            journal=frame_root / f"frame-{slot:03d}" / "journal.json",
            ack=frame_root / f"frame-{slot:03d}" / "parent-ack.json",
        ),
    )


def test_batch_selections_refuse_when_live_table_count_is_far_above_reviewed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The comparison is one-directional: a live count above the reviewed
    # count by more than the one-frame sliver tolerance has no benign
    # explanation and must still be refused, even though a live count below
    # the reviewed count is tolerated without limit elsewhere in this suite.
    mapping, records = _short_strip_mapping(9)
    reviewed = _reviewed_fingerprint_with_count(6)
    context = _batch_selection_context(mapping, records, reviewed)
    frames = _one_slot_batch(tmp_path, "far-from-reviewed", 1)

    monkeypatch.setattr(
        worker_module, "_derive_live_frame_selection", lambda *_a, **_k: context
    )
    monkeypatch.setattr(
        worker_module,
        "compare_selected_roll_fingerprint",
        lambda *_a, **_k: SimpleNamespace(matches=True, reason="matched"),
        raising=False,
    )
    monkeypatch.setattr(
        worker_module,
        "validate_live_0x8e_bytes",
        lambda table, _height: (table, len(records)),
    )
    monkeypatch.setattr(
        worker_module,
        "parse_live_transport_records_bytes",
        lambda *_a, **_k: records,
    )

    with pytest.raises(
        ProtocolError,
        match=r"live table has 9 scanner-addressable frame records, more than "
        r"one above the 6",
    ):
        worker_module._derive_live_batch_selections(
            [],
            b"fresh-preview",
            b"fresh-table",
            frames,
            reviewed_fingerprint=reviewed,
        )


def test_batch_selections_accept_one_addressable_sliver_beyond_reviewed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live table has one more addressable record than the reviewed
    # fingerprint described -- exactly the shape of a trailing sliver that
    # was too short to visually sign on the reviewed traversal but is still
    # an addressable transport slot on this one.
    mapping, records = _short_strip_mapping(7)
    reviewed = _reviewed_fingerprint_with_count(6)
    context = _batch_selection_context(mapping, records, reviewed)
    frames = _one_slot_batch(tmp_path, "one-sliver-beyond-reviewed", 1)
    bound_frames: list[int] = []

    monkeypatch.setattr(
        worker_module, "_derive_live_frame_selection", lambda *_a, **_k: context
    )
    monkeypatch.setattr(
        worker_module,
        "compare_selected_roll_fingerprint",
        lambda *_a, **_k: SimpleNamespace(matches=True, reason="matched"),
        raising=False,
    )
    monkeypatch.setattr(
        worker_module,
        "validate_live_0x8e_bytes",
        lambda table, _height: (table, len(records)),
    )
    monkeypatch.setattr(
        worker_module,
        "parse_live_transport_records_bytes",
        lambda *_a, **_k: records,
    )
    monkeypatch.setattr(
        worker_module,
        "_bind_plan_to_live_selection",
        lambda _plan, selection: bound_frames.append(selection.frame),
    )

    selections = worker_module._derive_live_batch_selections(
        [],
        b"fresh-preview",
        b"fresh-table",
        frames,
        reviewed_fingerprint=reviewed,
    )

    assert [selection.frame for selection in selections] == [1]
    assert bound_frames == [1]


def test_batch_selections_accept_a_live_count_several_frames_below_reviewed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: a live batch run over slots 3 and 20 of a reviewed
    # 40-frame roll, where the transport's trailing edge cleared the feeder
    # a few frames before the end and every record built from that jump was
    # excluded as unaddressable -- leaving 37 scanner-addressable records
    # against the reviewed fingerprint's 40. 0.1.3's plus-or-minus-one
    # comparison refused this batch outright even though both requested
    # slots were well within the addressable table; a live count several
    # frames below the reviewed count is the ordinary shape of a roll
    # ending, not a sign of a wrong or reordered roll, and must succeed.
    mapping, records = _short_strip_mapping(40, non_addressable_trailing=3)
    assert build_live_frame_table_payload(mapping)[2] == 37
    reviewed = _reviewed_fingerprint_with_count(40)
    context = _batch_selection_context(mapping, records, reviewed)
    frame_root = tmp_path / "several-below-reviewed"
    frames = tuple(
        worker_module.BatchFrameSpec(
            slot=slot,
            boundary_offset_rows=0,
            manual_review_approval=None,
            output=frame_root / f"frame-{slot:03d}" / "capture.bin",
            journal=frame_root / f"frame-{slot:03d}" / "journal.json",
            ack=frame_root / f"frame-{slot:03d}" / "parent-ack.json",
        )
        for slot in (3, 20)
    )
    bound_frames: list[int] = []

    monkeypatch.setattr(
        worker_module, "_derive_live_frame_selection", lambda *_a, **_k: context
    )
    monkeypatch.setattr(
        worker_module,
        "compare_selected_roll_fingerprint",
        lambda *_a, **_k: SimpleNamespace(matches=True, reason="matched"),
        raising=False,
    )
    monkeypatch.setattr(
        worker_module,
        "validate_live_0x8e_bytes",
        lambda table, _height: (table, len(records)),
    )
    monkeypatch.setattr(
        worker_module,
        "parse_live_transport_records_bytes",
        lambda *_a, **_k: records,
    )
    monkeypatch.setattr(
        worker_module,
        "_bind_plan_to_live_selection",
        lambda _plan, selection: bound_frames.append(selection.frame),
    )

    selections = worker_module._derive_live_batch_selections(
        [],
        b"fresh-preview",
        b"fresh-table",
        frames,
        reviewed_fingerprint=reviewed,
    )

    assert [selection.frame for selection in selections] == [3, 20]
    assert bound_frames == [3, 20]


def test_continuation_executor_runs_all_89_steps_with_fake_usb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(
            (100 + 143 * index for index in range(37)),
            start=1,
        )
    )
    mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)
    combined, _resolved = apply_batch_boundary_offsets(
        mapping,
        records,
        ((7, 9), (18, -11)),
    )
    plan = load_canonical_plan()
    geometry = _derive_index_geometry(plan)
    selection = SimpleNamespace(
        frame=18,
        frame_count=len(combined.origins),
        geometry=geometry,
        mapping=combined,
        selected=combined.origins[17],
        requested_boundary_offset_rows=-11,
        applied_boundary_offset_rows=-11,
        diagnostics=lambda: {"frame": 18, "prevalidated": True},
    )
    root = tmp_path / "continuation"
    root.mkdir()
    first = worker_module.BatchFrameSpec(
        7,
        9,
        root / "frame-007" / "capture.bin",
        root / "frame-007" / "journal.json",
        root / "frame-007" / "parent-ack.json",
    )
    second = worker_module.BatchFrameSpec(
        18,
        -11,
        root / "frame-018" / "capture.bin",
        root / "frame-018" / "journal.json",
        root / "frame-018" / "parent-ack.json",
    )
    batch = worker_module.LiveBatchJob(
        "continuation-session",
        root,
        (first, second),
        _reviewed_fingerprint(),
        1,
        2,
        CANONICAL_PLAN_SHA256,
        worker_module.CANONICAL_CONTINUATION_PLAN_SHA256,
        "c" * 64,
    )
    canonical_target = worker_module.validate_plan(plan)
    tiny_target = {
        **canonical_target,
        "repeat": 1,
        "request_len": 1,
        "request_parts": [1],
    }
    monkeypatch.setattr(worker_module, "EXPECTED_FINE_READS", 1)
    monkeypatch.setattr(worker_module, "METER_GROUP_BYTES", 5)
    monkeypatch.setattr(worker_module, "METER_CAPTURE_BYTES", 15)
    monkeypatch.setattr(worker_module, "validate_plan", lambda _plan: tiny_target)
    ready_groups: list[tuple[int, ...]] = []
    transactions: list[int] = []

    def ready(
        _ep_out: object,
        _ep_in: object,
        entries: list[dict],
        **_kwargs: object,
    ) -> tuple[int, int]:
        ready_groups.append(tuple(entry["seq"] for entry in entries))
        return 1, 0

    def perform(
        _ep_out: object,
        _ep_in: object,
        entry: dict,
        **_kwargs: object,
    ) -> TransactionResult:
        sequence = entry["seq"]
        transactions.append(sequence)
        if sequence in (
            *worker_module.METER_GET_WINDOW_SEQUENCES,
            *worker_module.FINE_GET_WINDOW_SEQUENCES,
        ):
            payload = bytes.fromhex(entry["expected_data_in"])
        elif sequence in worker_module.METER_READ_SEQUENCES or sequence == 607:
            payload = b"x"
        else:
            payload = bytes.fromhex(entry.get("expected_data_in", ""))
        return TransactionResult(
            phase=entry.get("expected_phase", 1),
            payload=payload,
            status=bytes.fromhex(entry.get("expected_status", "00" * 8)),
            sense=entry.get("expected_sense", "000000"),
            stall_recoveries=0,
        )

    accepted_proposal = SimpleNamespace(
        accepted=True,
        proposed_exposures=dict(worker_module.DEFAULT_EXPOSURES),
        refusals=(),
        to_dict=lambda: {"accepted": True},
    )
    accepted_final = SimpleNamespace(
        accepted=True,
        final_exposures=dict(worker_module.DEFAULT_EXPOSURES),
        refusals=(),
        to_dict=lambda: {"accepted": True},
    )
    monkeypatch.setattr(worker_module, "_perform_ready_group", ready)
    monkeypatch.setattr(worker_module, "_perform_with_busy_retry", perform)
    monkeypatch.setattr(
        worker_module,
        "observe_meter_pass",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "propose_next_exposures",
        lambda *_args, **_kwargs: accepted_proposal,
    )
    monkeypatch.setattr(
        worker_module,
        "verify_final_convergence",
        lambda *_args, **_kwargs: accepted_final,
    )
    monkeypatch.setattr(
        worker_module,
        "_wait_post_scan_ready",
        lambda *_args, **_kwargs: (1, 0),
    )
    density_evidence = SimpleNamespace(
        source_binding=SimpleNamespace(session_id=batch.session_id)
    )
    monkeypatch.setattr(
        worker_module,
        "_density_frame_ownership_receipt",
        lambda *_args, **_kwargs: {"fixture": "owned"},
    )

    with pytest.raises(ProtocolError, match="another reservation"):
        worker_module._run_live_continuation_frame(
            "out",
            "in",
            plan,
            tmp_path / "plan.jsonl",
            CANONICAL_PLAN_SHA256,
            load_canonical_continuation_plan(),
            worker_module.CANONICAL_CONTINUATION_PLAN_SHA256,
            second,
            selection,
            batch_job=batch,
            frame_index=2,
            lifecycle=worker_module.SessionLifecycle(),
            density_calibration=_density_calibration("another-reservation"),
            density_evidence=density_evidence,
            actual_usb_bus=1,
            actual_usb_address=2,
        )
    assert not second.output.parent.exists()

    journal = worker_module._run_live_continuation_frame(
        "out",
        "in",
        plan,
        tmp_path / "plan.jsonl",
        CANONICAL_PLAN_SHA256,
        load_canonical_continuation_plan(),
        worker_module.CANONICAL_CONTINUATION_PLAN_SHA256,
        second,
        selection,
        batch_job=batch,
        frame_index=2,
        lifecycle=worker_module.SessionLifecycle(),
        density_calibration=_density_calibration(batch.session_id),
        density_evidence=density_evidence,
        actual_usb_bus=1,
        actual_usb_address=2,
    )

    assert len(ready_groups) == 15
    assert len(transactions) - 1 + len(ready_groups) == 89
    assert 174 not in transactions
    assert 500 not in transactions
    assert transactions[-1] == 607
    assert journal["status"] == "frame-complete"
    assert journal["frame_complete"] is True
    assert journal["session_reservation_retained"] is True
    assert journal["unit_released"] is False
    assert journal["density_calibration_session_id"] == batch.session_id
    assert second.output.read_bytes() == b"x"
    assert second.journal.read_text(encoding="utf-8") == (
        json.dumps(journal, indent=2, sort_keys=True) + "\n"
    )


def test_live_two_frame_batch_uses_one_combined_table_and_one_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = load_canonical_plan()
    canonical_target = worker_module.validate_plan(plan)
    tiny_target = {
        **canonical_target,
        "repeat": 1,
        "request_len": 1,
        "request_parts": [1],
    }
    for sequence in worker_module.PREVIEW_READ_SEQUENCES:
        entry = plan[sequence - 1]
        entry["request_len"] = 1
        entry["request_parts"] = [1]

    records = tuple(
        TransportRecord(
            row=row,
            code=6 * (row % 18),
            selector=row // 18,
            native_origin=42 * row,
        )
        for row in range(6_000)
    )
    origins = tuple(
        NativeFrameOrigin(
            frame=frame,
            boundary_index=frame - 1,
            boundary_output_row=row - 4,
            lookup_row=row,
            code=records[row].code,
            selector=records[row].selector,
            native_origin=records[row].native_origin,
            method="direct-gap-trailing-row",
            automatic=True,
            manual_review=False,
            review_reasons=(),
            affine_residual_rows=0.0,
        )
        for frame, row in enumerate(
            (100 + 143 * index for index in range(37)),
            start=1,
        )
    )
    base_mapping = TransportMapping(6_000, 0.0, 42.0, 0.0, 0.0, origins)
    combined, resolved = apply_batch_boundary_offsets(
        base_mapping,
        records,
        ((7, 9), (18, -11)),
    )
    geometry = SimpleNamespace(
        requested_resolution=97,
        native_resolution=4_000,
        pitch=41,
        native_width=3_946,
        native_height=250_278,
        width=96,
        height=len(worker_module.PREVIEW_READ_SEQUENCES),
        block_bytes=1,
        expected_stream_bytes=len(worker_module.PREVIEW_READ_SEQUENCES),
    )

    def selection_for(
        slot: int,
        offset: int,
        pair: tuple[NativeFrameOrigin, NativeFrameOrigin],
    ) -> SimpleNamespace:
        base, selected = pair
        return SimpleNamespace(
            frame=slot,
            frame_count=len(combined.origins),
            geometry=geometry,
            mapping=combined,
            base_selected=base,
            selected=selected,
            requested_boundary_offset_rows=offset,
            applied_boundary_offset_rows=offset,
            diagnostics=lambda: {
                "frame": slot,
                "boundary_offset": {
                    "requested_rows": offset,
                    "applied_rows": offset,
                },
                "selected": {
                    "frame": slot,
                    "lookup_row": selected.lookup_row,
                    "native_origin": selected.native_origin,
                },
            },
        )

    selections = (
        selection_for(7, 9, resolved[0]),
        selection_for(18, -11, resolved[1]),
    )
    root = tmp_path / "successful-batch"
    root.mkdir()
    first = worker_module.BatchFrameSpec(
        7,
        9,
        root / "frame-007" / "capture.bin",
        root / "frame-007" / "journal.json",
        root / "frame-007" / "parent-ack.json",
    )
    second = worker_module.BatchFrameSpec(
        18,
        -11,
        root / "frame-018" / "capture.bin",
        root / "frame-018" / "journal.json",
        root / "frame-018" / "parent-ack.json",
    )
    first.output.parent.mkdir()
    batch = worker_module.LiveBatchJob(
        "successful-batch",
        root,
        (first, second),
        _reviewed_fingerprint(),
        1,
        2,
        CANONICAL_PLAN_SHA256,
        worker_module.CANONICAL_CONTINUATION_PLAN_SHA256,
        "c" * 64,
    )
    nonce = "offline-batch-nonce"
    first.ack.write_text(
        json.dumps(
            {
                "ack_nonce": nonce,
                "action": "continue",
                "frame_index": 1,
                "schema_version": 1,
                "session_id": batch.session_id,
                "slot": first.slot,
            }
        ),
        encoding="utf-8",
    )

    startup = bytearray(10 + 37 * 8)
    startup[:4] = b"\x8f\0\0\0"
    startup[4:6] = (len(startup) - 6).to_bytes(2, "big")
    startup[6:8] = (len(startup) - 8).to_bytes(2, "big")
    startup[8] = 37
    header_8e = b"\0\x8e\0\0\0\x06"
    prevalidated = False
    reserves: list[int] = []
    sent_tables: list[bytes] = []
    transactions: list[int] = []
    fine_reads: list[int] = []
    ready_groups: list[tuple[int, ...]] = []
    ack_boundaries: list[tuple[int, int, int, int]] = []
    releases: list[tuple[object, object]] = []
    usb_events: list[str] = []

    class USBUtil:
        @staticmethod
        def release_interface(_device: object, _number: int) -> None:
            usb_events.append("interface-released")

        @staticmethod
        def dispose_resources(_device: object) -> None:
            usb_events.append("resources-disposed")

    ep_out = SimpleNamespace(bEndpointAddress=0x01)
    ep_in = SimpleNamespace(bEndpointAddress=0x82)
    interface = SimpleNamespace(bInterfaceNumber=0)

    def derive_batch(
        _plan: list[dict],
        preview: bytes,
        table: bytes,
        frames: tuple[worker_module.BatchFrameSpec, ...],
        *,
        reviewed_fingerprint: ReviewedRollFingerprint,
    ) -> tuple[SimpleNamespace, ...]:
        nonlocal prevalidated
        assert len(preview) == len(worker_module.PREVIEW_READ_SEQUENCES)
        assert table == header_8e
        assert frames == batch.frames
        assert reviewed_fingerprint == batch.reviewed_fingerprint
        prevalidated = True
        return selections

    def perform(
        _ep_out: object,
        _ep_in: object,
        entry: dict,
        **_kwargs: object,
    ) -> TransactionResult:
        sequence = entry["seq"]
        transactions.append(sequence)
        if sequence == 17:
            reserves.append(sequence)
        if sequence == 174:
            assert prevalidated is True
            sent_tables.append(bytes.fromhex(entry["data_out"]))
        if sequence in (115, 116, 117):
            payload = b"window"
        elif sequence in worker_module.PREVIEW_READ_SEQUENCES:
            payload = b"p"
        elif sequence == 171:
            payload = header_8e
        elif sequence == 172:
            payload = header_8e
        elif sequence in (
            *worker_module.METER_GET_WINDOW_SEQUENCES,
            *worker_module.FINE_GET_WINDOW_SEQUENCES,
        ):
            payload = bytes.fromhex(entry["expected_data_in"])
        elif sequence in worker_module.METER_READ_SEQUENCES:
            payload = b"m"
        elif sequence == 607:
            fine_reads.append(sequence)
            payload = b"f"
        else:
            payload = bytes.fromhex(entry.get("expected_data_in", ""))
        return TransactionResult(
            phase=entry.get("expected_phase", 1),
            payload=payload,
            status=bytes.fromhex(entry.get("expected_status", "00" * 8)),
            sense=entry.get("expected_sense", "000000"),
            stall_recoveries=0,
        )

    def perform_startup(
        _ep_out: object,
        _ep_in: object,
        entry: dict,
        **_kwargs: object,
    ) -> TransactionResult:
        assert entry["seq"] == worker_module.VARIABLE_FRAME_TABLE_SEQUENCE
        return TransactionResult(
            phase=3,
            payload=bytes(startup),
            status=bytes(8),
            sense="000000",
            stall_recoveries=0,
        )

    def ready(
        _ep_out: object,
        _ep_in: object,
        entries: list[dict],
        **_kwargs: object,
    ) -> tuple[int, int]:
        ready_groups.append(tuple(entry["seq"] for entry in entries))
        return 1, 0

    real_wait_for_parent_ack = worker_module.wait_for_parent_ack

    def acknowledge(
        path: Path,
        *,
        session_id: str,
        frame_index: int,
        slot: int,
        nonce: str,
        timeout_seconds: float = 1_800.0,
        poll_seconds: float = 0.1,
    ) -> str:
        del timeout_seconds, poll_seconds
        ack_boundaries.append((frame_index, slot, len(transactions), len(ready_groups)))
        if frame_index == 2:
            path.write_text(
                json.dumps(
                    {
                        "ack_nonce": nonce,
                        "action": "continue",
                        "frame_index": frame_index,
                        "schema_version": 1,
                        "session_id": session_id,
                        "slot": slot,
                    }
                ),
                encoding="utf-8",
            )
        return real_wait_for_parent_ack(
            path,
            session_id=session_id,
            frame_index=frame_index,
            slot=slot,
            nonce=nonce,
            timeout_seconds=0,
            poll_seconds=0,
        )

    def release(ep_out_value: object, ep_in_value: object) -> TransactionResult:
        releases.append((ep_out_value, ep_in_value))
        return TransactionResult(1, b"", bytes(8), "000000", 0)

    preview_windows = [
        {
            "color_id": color,
            "resx": 97,
            "resy": 97,
            "upper_left_x": 0,
            "upper_left_y": 0,
            "width": 3_946,
            "height": 250_278,
            "bit_depth": 16,
            "exposure_raw_10ns": 70_000 + color,
        }
        for color in (1, 2, 3)
    ]
    accepted_proposal = SimpleNamespace(
        accepted=True,
        proposed_exposures=dict(worker_module.DEFAULT_EXPOSURES),
        refusals=(),
        to_dict=lambda: {"accepted": True},
    )
    accepted_final = SimpleNamespace(
        accepted=True,
        final_exposures=dict(worker_module.DEFAULT_EXPOSURES),
        refusals=(),
        to_dict=lambda: {"accepted": True},
    )
    monkeypatch.setattr(worker_module, "EXPECTED_FINE_READS", 1)
    monkeypatch.setattr(
        worker_module,
        "EXPECTED_PREVIEW_BYTES",
        len(worker_module.PREVIEW_READ_SEQUENCES),
    )
    monkeypatch.setattr(worker_module, "METER_GROUP_BYTES", 5)
    monkeypatch.setattr(worker_module, "METER_CAPTURE_BYTES", 15)
    monkeypatch.setattr(worker_module, "validate_plan", lambda _plan: tiny_target)
    monkeypatch.setattr(worker_module, "_derive_index_geometry", lambda _plan: geometry)
    monkeypatch.setattr(
        worker_module, "_validate_scanner_identity", lambda _payload: None
    )
    monkeypatch.setattr(
        worker_module, "_validate_live_preview_windows", lambda *_args: preview_windows
    )
    monkeypatch.setattr(
        worker_module,
        "build_nikon_density_evidence",
        lambda *_args, **kwargs: SimpleNamespace(
            source_binding=SimpleNamespace(session_id=kwargs["session_id"]),
            preview_identity_sha256="d" * 64,
            to_dict=lambda: {"scope": "reservation-preview", "test_fixture": True},
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "_density_frame_ownership_receipt",
        lambda *_args, **_kwargs: {"fixture": "owned"},
    )
    monkeypatch.setattr(worker_module, "_derive_live_batch_selections", derive_batch)
    monkeypatch.setattr(worker_module, "_perform_with_busy_retry", perform)
    monkeypatch.setattr(
        worker_module, "_perform_variable_frame_table_transaction", perform_startup
    )
    monkeypatch.setattr(worker_module, "_perform_ready_group", ready)
    monkeypatch.setattr(
        worker_module, "_wait_post_scan_ready", lambda *_args, **_kwargs: (1, 0)
    )
    monkeypatch.setattr(
        worker_module, "observe_meter_pass", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        worker_module,
        "propose_next_exposures",
        lambda *_args, **_kwargs: accepted_proposal,
    )
    monkeypatch.setattr(
        worker_module,
        "verify_final_convergence",
        lambda *_args, **_kwargs: accepted_final,
    )
    monkeypatch.setattr(worker_module, "wait_for_parent_ack", acknowledge)
    monkeypatch.setattr(worker_module, "_release_unit", release)
    monkeypatch.setattr(worker_module.secrets, "token_hex", lambda _size: nonce)
    monkeypatch.setattr(
        worker_module,
        "_connect_device",
        lambda **_kwargs: (
            SimpleNamespace(bus=1, address=2),
            interface,
            ep_out,
            ep_in,
            USBUtil,
        ),
    )

    session_journal_path = root / "session-journal.json"
    worker_module.run_live_capture(
        plan,
        tmp_path / "plan.jsonl",
        CANONICAL_PLAN_SHA256,
        first.output,
        first.journal,
        1,
        frame=first.slot,
        boundary_offset_rows=first.boundary_offset_rows,
        batch_job=batch,
        continuation_plan=load_canonical_continuation_plan(),
        continuation_plan_sha256=(worker_module.CANONICAL_CONTINUATION_PLAN_SHA256),
        session_journal_path=session_journal_path,
    )

    assert reserves == [17]
    assert len(sent_tables) == 1
    retained_table = sent_tables[0]
    for slot in (7, 18):
        origin = combined.origins[slot - 1]
        assert struct.unpack_from(">IHH", retained_table, 4 + (slot - 1) * 8) == (
            origin.native_origin,
            origin.selector,
            origin.code,
        )
    assert fine_reads == [607, 607]
    assert [(frame, slot) for frame, slot, _tx, _ready in ack_boundaries] == [
        (1, 7),
        (2, 18),
    ]
    first_boundary = ack_boundaries[0]
    second_boundary = ack_boundaries[1]
    continuation_transactions = second_boundary[2] - first_boundary[2]
    continuation_ready_groups = second_boundary[3] - first_boundary[3]
    assert continuation_transactions - 1 + continuation_ready_groups == 89
    assert 174 not in transactions[first_boundary[2] : second_boundary[2]]
    assert releases == [(ep_out, ep_in)]
    first_receipt = json.loads(first.journal.read_text(encoding="utf-8"))
    assert first_receipt["status"] == "frame-complete"
    assert first_receipt["session_reservation_retained"] is True
    assert first_receipt["unit_released"] is False
    assert first_receipt["nikon_density_calibration"]["numerators_rgb"] == [
        57_980,
        48_356,
        32_854,
    ]
    assert first_receipt["nikon_density_calibration"]["payload_hex_rgb"] == [
        "8c20000000040000e27c",
        "8c20000000040000bce4",
        "8c200000000400008056",
    ]
    assert first_receipt["nikon_density_calibration"]["session_id"] == (
        batch.session_id
    )
    assert first_receipt["density_calibration_session_id"] == batch.session_id
    second_receipt = json.loads(second.journal.read_text(encoding="utf-8"))
    assert second_receipt["status"] == "frame-complete"
    assert second_receipt["batch_session"] == {
        "frame_index": 2,
        "frame_total": 2,
        "selected_slots": [7, 18],
        "session_id": batch.session_id,
    }
    assert second_receipt["continuation_plan_sha256"] == (
        worker_module.CANONICAL_CONTINUATION_PLAN_SHA256
    )
    assert second_receipt["session_reservation_retained"] is True
    assert second_receipt["unit_released"] is False
    assert (
        second_receipt["nikon_density_calibration"]
        == first_receipt["nikon_density_calibration"]
    )
    assert second_receipt["density_calibration_session_id"] == batch.session_id
    assert second.output.read_bytes() == b"f"
    session = json.loads(session_journal_path.read_text(encoding="utf-8"))
    assert session["status"] == "complete"
    assert session["completed_slots"] == [7, 18]
    assert session["reservation_acquired"] is True
    assert session["unit_release_attempts"] == 1
    assert session["unit_released"] is True
    assert session["recovery_required"] == "none"
    assert session["batch_job_sha256"] == batch.job_sha256
    assert session["capture_engine_sha256"] == worker_module.CAPTURE_WORKER_SHA256
    assert session["capture_bundle_sha256"] == worker_module.CAPTURE_BUNDLE_SHA256
    assert (
        session["nikon_density_calibration"]
        == first_receipt["nikon_density_calibration"]
    )
    assert session["density_calibration_session_id"] == batch.session_id
    assert usb_events == ["interface-released", "resources-disposed"]
