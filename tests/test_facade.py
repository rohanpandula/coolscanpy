"""Hardware-free contracts for the public facade (``coolscanpy.__init__``).

Follows this suite's own convention (see tests/conftest.py): every test
fakes the boundary just below the module under test with small classes
local to this file. The doubles below are adapted from the concrete tests
they mirror -- ``tests/session/test_service.py``'s ``FakeBackend``,
``tests/roll/test_ls5000_roll_session.py``'s synthetic preview raster, and
``tests/protocol/ls5000_single_pass/test_capture_process.py``'s
``FakeRunner``/``FakeBatchProcess`` -- so the facade is exercised against the
same replay-fixture shapes the concrete modules are already tested with, not
a parallel invented double.
"""

from __future__ import annotations

import hashlib
import functools
import json
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pytest

import coolscanpy
import coolscanpy._device as device_module
from coolscanpy._roll import Roll
from coolscanpy.capture.single_pass_workflow import LS5000SinglePassWorkflow, PackedCaptureContract
from coolscanpy.protocol.ls5000_single_pass import roll_index
from coolscanpy.protocol.ls5000_single_pass.bundle import CAPTURE_BUNDLE_SHA256
from coolscanpy.protocol.ls5000_single_pass.capture_process import (
    CANONICAL_FINE_READ_BYTES,
    CANONICAL_FINE_READ_COUNT,
    CaptureProcessAdapter,
)
from coolscanpy.protocol.ls5000_single_pass.continuation_plan import CANONICAL_CONTINUATION_PLAN_SHA256
from coolscanpy.protocol.ls5000_single_pass.plan import CANONICAL_PLAN_SHA256
from coolscanpy.receipts.quality import StoppedTransportSmearAssessment
from coolscanpy.session.backend import ScanMode, ScannerCapabilities, ScannerDevice
from coolscanpy.session.params import ScanParams
from coolscanpy.session.result import ScanResult
from coolscanpy.session.service import ScannerService


# ---------------------------------------------------------------------------
# Plain-scan (Device) doubles -- mirrors tests/session/test_service.py's
# FakeBackend.
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, devices: list[ScannerDevice] | None = None) -> None:
        self._devices = devices or []
        self.eject_calls: list[str] = []
        self.eject_result: bool = True
        self.eject_error: Exception | None = None
        self.scan_calls: list[ScanParams] = []

    def list_devices(self) -> list[ScannerDevice]:
        return self._devices

    def scan(self, device_id: str, params: ScanParams, progress, cancel: threading.Event) -> ScanResult:
        self.scan_calls.append(params)
        if progress:
            progress(0.0)
        if cancel.is_set():
            raise RuntimeError("scan cancelled")
        rgb = np.full((8, 6, 3), 12_000, dtype=np.uint16)
        if progress:
            progress(1.0)
        return ScanResult(rgb=rgb, ir=None, dpi=params.dpi, device_model="Fake LS-5000")

    def eject(self, device_id: str) -> bool:
        device = next((d for d in self._devices if d.id == device_id), None)
        if device is None or not device.capabilities.can_eject:
            return False
        self.eject_calls.append(device_id)
        if self.eject_error is not None:
            raise self.eject_error
        return self.eject_result


_COOLSCAN_ID = "net:scanner:coolscan3:usb:001:002"


def _caps(**overrides: object) -> ScannerCapabilities:
    values: dict[str, object] = dict(
        ir_channel=True,
        supported_dpi=(1_000, 2_000, 4_000),
        supported_depths=(8, 16),
        sources=(ScanMode.NEGATIVE,),
        multi_sample=True,
        adapter_frame_capacity=40,
        adapter_frame_control=True,
        auto_exposure=True,
        registered_geometry=False,
        can_eject=True,
    )
    values.update(overrides)
    return ScannerCapabilities(**values)


def _coolscan_device(**cap_overrides: object) -> ScannerDevice:
    return ScannerDevice(
        id=_COOLSCAN_ID,
        vendor="Nikon",
        model="LS-5000 ED",
        capabilities=_caps(**cap_overrides),
    )


@pytest.fixture(autouse=True)
def _reset_open_device_registry() -> None:
    """The in-process open-device registry is module-level state; keep it
    from leaking between tests."""

    device_module._open_devices.clear()
    yield
    device_module._open_devices.clear()


@pytest.fixture
def fake_service_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[ScannerDevice]], _FakeBackend]:
    """Monkeypatch coolscanpy's device factory to hand out a ScannerService
    wrapping a fake backend, exactly like ScannerService's own tests do
    (``service._backend = FakeBackend(...)``) -- avoids ever importing
    python-sane or touching hardware."""

    created: dict[str, _FakeBackend] = {}

    def install(devices: list[ScannerDevice]) -> _FakeBackend:
        backend = _FakeBackend(devices)
        created["backend"] = backend

        def factory() -> ScannerService:
            service = ScannerService()
            service._backend = backend
            return service

        monkeypatch.setattr(device_module, "_service_factory", factory)
        return backend

    return install


# ---------------------------------------------------------------------------
# Roll-engine doubles -- mirrors tests/roll/test_ls5000_roll_session.py's
# synthetic index/table encoders and
# tests/protocol/ls5000_single_pass/test_capture_process.py's FakeRunner /
# FakeBatchProcess.
# ---------------------------------------------------------------------------


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _encode_index(rgb16: np.ndarray) -> bytes:
    blocks = np.zeros((rgb16.shape[0] // 2, roll_index.INDEX_BLOCK_WORDS), dtype=np.uint16)
    blocks[:, 0:96] = rgb16[0::2, :, 0]
    blocks[:, 96:192] = rgb16[0::2, :, 1]
    blocks[:, 192:288] = rgb16[0::2, :, 2]
    blocks[:, 512:608] = rgb16[1::2, :, 0]
    blocks[:, 608:704] = rgb16[1::2, :, 1]
    blocks[:, 704:800] = rgb16[1::2, :, 2]
    blocks[:, 800::2] = roll_index.INDEX_TRAILER_MARK
    blocks[:, 801::2] = np.arange(
        roll_index.INDEX_TRAILER_COUNTER0,
        roll_index.INDEX_TRAILER_COUNTER0 + roll_index.INDEX_TRAILER_WORDS // 2,
        dtype=np.uint16,
    )
    return blocks.astype(">u2", copy=False).tobytes()


def _synthetic_index(*, height: int = 6_104) -> np.ndarray:
    """40 textured cells separated by clear-film gaps (adapted from
    tests/roll/test_ls5000_roll_session.py's ``_synthetic_index``)."""

    pitch = 143
    leader = 128
    boundaries = [leader + index * pitch for index in range(41)]
    y = np.arange(height, dtype=np.int64)[:, None]
    x = np.arange(90, dtype=np.int64)[None, :]
    texture = (x * 173 + y * 71 + (x * y) % 997) % 7_000
    aperture = np.empty((height, 90, 3), dtype=np.int64)
    for channel, base in enumerate((7_000, 5_500, 4_000)):
        aperture[:, :, channel] = base + texture * (3 - channel) // 2
    clear_base = np.asarray((34_200, 25_500, 17_800), dtype=np.int64)
    clear_noise = ((x * 19 + y * 13) % 301 - 150)[:, :, None]
    aperture[: boundaries[0]] = clear_base + clear_noise[: boundaries[0]]
    aperture[boundaries[-1] :] = clear_base + clear_noise[boundaries[-1] :]
    for boundary in boundaries:
        aperture[boundary - 3 : boundary + 3] = clear_base + clear_noise[boundary - 3 : boundary + 3]
    rgb = np.empty((height, 96, 3), dtype=np.int64)
    rgb[:, 2:92] = aperture
    rgb[:, :2] = np.asarray((1_300, 1_000, 700))
    rgb[:, 92:] = np.asarray((1_100, 850, 600))
    return rgb.clip(0, 65_535).astype(np.uint16)


def _transport_table(rows: int) -> bytes:
    records = bytearray()
    for row in range(rows):
        records.extend(struct.pack(">HH", 6 * (row % 18), row // 18))
    total = 8 + len(records)
    return b"\x00\x8e\x00\x00" + total.to_bytes(2, "big") + b"\x00\x00" + bytes(records)


def _arg(argv: Sequence[str], name: str) -> str:
    return argv[argv.index(name) + 1]


@dataclass
class _PreviewAndBatchWorker:
    """Combined ProcessRunner (preview attempts) + BatchProcessSpawner (fine
    scans) double. Writes real journals/streams the way the real worker
    subprocess would, so ``CaptureProcessAdapter``/``build_roll_preview_session``
    exercise their real validation logic."""

    worker_sha256: str
    events: list[str] = field(default_factory=list)

    # -- ProcessRunner (preview) --------------------------------------

    def __call__(self, argv: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        assert "--preview-only" in argv, "this double only fakes preview attempts"
        output = Path(_arg(argv, "--output"))
        journal_path = Path(_arg(argv, "--journal"))
        rgb = _synthetic_index()
        preview = _encode_index(rgb)
        table = _transport_table(len(rgb))
        preview_path = cwd / "capture-preview.bin"
        table_path = cwd / "capture-008e.bin"
        mapping_path = cwd / "capture-frame-map.json"
        preview_path.write_bytes(preview)
        table_path.write_bytes(table)
        output.write_bytes(b"")
        mapping = {
            "status": "preview-only-complete",
            "slot_capacity_hint": 40,
            "slot_capacity_semantics": "scanner-addressable preview slots; not an exposure count",
            "preview_bytes": len(preview),
            "preview_sha256": _sha256(preview),
            "table_bytes": len(table),
            "table_sha256": _sha256(table),
            "frame_detection": "deferred-offline",
        }
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
        journal = {
            "status": "complete",
            "capture_mode": "preview-only",
            "requested_frame": None,
            "requested_boundary_offset_rows": 0,
            "expected_frame_count": None,
            "expected_reads": 0,
            "completed_reads": 0,
            "expected_bytes": 0,
            "completed_bytes": 0,
            "disk_bytes": 0,
            "unit_released": True,
            "recovery_required": None,
            "output": str(output.resolve()),
            "output_sha256": _sha256(b""),
            "plan_sha256": CANONICAL_PLAN_SHA256,
            "capture_engine_sha256": self.worker_sha256,
            "scanner_identity": "Nikon LS-5000 ED 1.03",
            "preview_geometry_validated_before_reads": True,
            "preview_windows": [
                {
                    "color_id": color,
                    "resolution": [97, 97],
                    "origin": [0, 0],
                    "size": [3_946, 250_278],
                    "bit_depth": 16,
                }
                for color in (1, 2, 3)
            ],
            "live_startup_0x8f": {"count": 40},
            "live_index_artifacts": {
                "mapping": str(mapping_path.resolve()),
                "preview": str(preview_path.resolve()),
                "table": str(table_path.resolve()),
            },
            "live_index_evidence": {
                "status": "persisted-before-frame-detection",
                "preview_bytes": len(preview),
                "preview_sha256": _sha256(preview),
                "table_bytes": len(table),
                "table_sha256": _sha256(table),
            },
            "preview_only_receipt": mapping,
        }
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        self.events.append("preview")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def _roll_identity_payload(reviewed_sha: str, *, slot: int) -> dict[str, Any]:
    return {
        "reviewed_fingerprint_sha256": reviewed_sha,
        "fresh_fingerprint_sha256": reviewed_sha,
        "comparison": {
            "matches": True,
            "reason": "matched",
            "compared_frames": 40,
            "preview_height_delta_rows": 0,
            "visual_median_hamming": 0.0,
            "visual_p90_hamming": 0,
            "frame_start_median_delta_rows": 0.0,
            "frame_start_max_delta_rows": 0,
            "native_origin_median_delta": 0.0,
            "native_origin_max_delta": 0,
            "discriminative_frames": 40,
            "minimum_discriminative_frames": 3,
            "minimum_visual_log_span": 0.5,
        },
        "selected_slot_comparison": {
            "matches": True,
            "reason": "matched",
            "slot": slot,
            "visual_hamming": 0,
            "maximum_visual_hamming": 48,
            "reviewed_visual_log_span": 2.0,
            "fresh_visual_log_span": 2.0,
            "minimum_visual_log_span": 0.5,
        },
    }


@functools.lru_cache(maxsize=1)
def _zero_stream_sha256(size: int) -> str:
    """sha256 of ``size`` NUL bytes, computed in memory (no disk read).

    A packed-stream file created via ``open(path, "xb"); f.truncate(size)``
    without writing anything is a sparse hole that reads back as all zero
    bytes (standard POSIX truncate semantics) -- this equals what the real
    ``stable_hasher`` would compute for that file, without needing to read
    hundreds of megabytes back from disk.
    """

    digest = hashlib.sha256()
    chunk = b"\x00" * (8 * 1024 * 1024)
    remaining = size
    while remaining > 0:
        take = min(remaining, len(chunk))
        digest.update(chunk[:take])
        remaining -= take
    return digest.hexdigest()


_FULL_STREAM_BYTES = CANONICAL_FINE_READ_COUNT * CANONICAL_FINE_READ_BYTES


def _fake_decoded_frame() -> np.ndarray:
    """One full-size (height, width, 4) uint16 array the fake decoder always
    returns, regardless of the (sparse, content-irrelevant) stream bytes on
    disk. Cached so every frame in a test reuses the same allocation."""

    cached = getattr(_fake_decoded_frame, "_cache", None)
    if cached is None:
        rng = np.random.default_rng(20260716)
        cached = rng.integers(4_000, 55_000, size=(5_959, 3_946, 4), dtype=np.uint16)
        _fake_decoded_frame._cache = cached
    return cached


def _make_workflow() -> LS5000SinglePassWorkflow:
    """Real contract (so the packed-stream byte count matches the adapter's
    own canonical check), fake decoder + smear assessor + hasher (so no test
    needs a byte-perfect packed stream or a slow full-file hash)."""

    return LS5000SinglePassWorkflow(
        contract=PackedCaptureContract(),
        decoder=lambda _path: (
            _fake_decoded_frame(),
            {"padding_validated_records": CANONICAL_FINE_READ_COUNT, "rgb_samples_decoded": 4, "ir_planes_transferred": 1},
        ),
        smear_assessor=lambda _rgb, *, dpi: StoppedTransportSmearAssessment(
            verdict="clean",
            start_row=None,
            suffix_rows=0,
            minimum_matches=64,
            tail_median_rms=None,
            tail_min_corr=None,
            pre_tail_median_rms=None,
            texture_span=1_234.0,
            reason="unit-test clean",
        ),
        # NOT overriding stable_hasher: the real hasher's output must agree
        # with write_full_negative_tiff's own (also real) reported TIFF
        # hashes, which _verify_outputs cross-checks. The packed stream is a
        # sparse file (see _FULL_STREAM_BYTES truncate() below), so hashing
        # it is memory-bandwidth-bound, not disk-bound, and stays fast.
    )


@dataclass
class _FakeBatchProcess:
    """RunningBatchProcess double: emits one frame at a time, waiting for the
    parent's ack file before advancing -- mirrors
    tests/protocol/ls5000_single_pass/test_capture_process.py's
    FakeBatchProcess."""

    job_path: Path
    session_journal_path: Path
    events: list[str]
    stop_after_index: int | None = None

    def __post_init__(self) -> None:
        self.job = json.loads(self.job_path.read_text(encoding="utf-8"))
        self.index = 0
        self.returncode: int | None = None
        self._emit_frame()

    def _emit_frame(self) -> None:
        frame = self.job["frames"][self.index]
        directory = self.job_path.parent
        output = directory / frame["output"]
        journal_path = directory / frame["journal"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.truncate(_FULL_STREAM_BYTES)
        output_sha256 = _zero_stream_sha256(_FULL_STREAM_BYTES)
        reviewed_sha = self.job["reviewed_roll_fingerprint"]["binding_sha256"]
        journal = {
            "ack_nonce": f"nonce-{frame['slot']}",
            "batch_session": {
                "frame_index": self.index + 1,
                "frame_total": len(self.job["frames"]),
                "selected_slots": [item["slot"] for item in self.job["frames"]],
                "session_id": self.job["session_id"],
            },
            "capture_engine_sha256": _sha256(_FAKE_WORKER_SOURCE),
            "capture_mode": "full",
            "completed_bytes": _FULL_STREAM_BYTES,
            "completed_reads": CANONICAL_FINE_READ_COUNT,
            "continuation_plan_sha256": CANONICAL_CONTINUATION_PLAN_SHA256,
            "disk_bytes": _FULL_STREAM_BYTES,
            "expected_bytes": _FULL_STREAM_BYTES,
            "expected_reads": CANONICAL_FINE_READ_COUNT,
            "frame_complete": True,
            "status": "frame-complete",
            "unit_released": False,
            "recovery_required": None,
            "output": str(output.resolve()),
            "output_sha256": output_sha256,
            "plan_sha256": CANONICAL_PLAN_SHA256,
            "requested_frame": frame["slot"],
            "requested_boundary_offset_rows": frame["boundary_offset_rows"],
            "applied_boundary_offset_rows": frame["boundary_offset_rows"],
            "resolved_lookup_row": 2_400 + frame["boundary_offset_rows"],
            "resolved_native_origin": 100_000 + frame["slot"],
            "scanner_identity": "Nikon LS-5000 ED 1.03",
            "preview_geometry_validated_before_reads": True,
            "session_reservation_retained": True,
            "reviewed_roll_fingerprint_sha256": reviewed_sha,
            "manual_review_approval": frame["manual_review_approval"],
            "live_frame_selection": {
                "frame": frame["slot"],
                "preview_sha256": "1" * 64,
                "table_sha256": "2" * 64,
                "detection": {"confidence": "automatic", "frame_count": 40},
                "transport_mapping": {"status": "resolved"},
                "roll_identity": _roll_identity_payload(reviewed_sha, slot=frame["slot"]),
                "selected": {
                    "automatic": frame["manual_review_approval"] is None,
                    "manual_review": frame["manual_review_approval"] is not None,
                    "frame": frame["slot"],
                    "native_origin": 100_000 + frame["slot"],
                    "method": "explicit-addressable-slot",
                    "selector": 1,
                    "code": 1,
                    "lookup_row": 2_400 + frame["boundary_offset_rows"],
                },
            },
            "fine_set_windows_preflight": [
                {
                    "color_id": color,
                    "resolution": [4_000, 4_000],
                    "origin": [0, 100_000 + frame["slot"]],
                    "size": [3_946, 5_959],
                    "samples": 4,
                    "exposure_raw_10ns": 100_000 + color,
                }
                for color in (1, 2, 3, 9)
            ],
            "fine_windows": [
                {
                    "color_id": color,
                    "resolution": [4_000, 4_000],
                    "origin": [0, 100_000 + frame["slot"]],
                    "size": [3_946, 5_959],
                    "samples": 4,
                    "exposure_raw_10ns": 100_000 + color,
                    "interleave": 64,
                }
                for color in (1, 2, 3, 9)
            ],
            "meter_controller_final_result": {
                "accepted": True,
                "final_exposures_raw_10ns": {"R": 100_001, "G": 100_002, "B": 100_003, "IR": 100_009},
            },
            "meter_evidence_persisted_before_fine_arm": True,
            "meter_final_exposures": {
                "controller_channels_raw_10ns": {"R": 100_001, "G": 100_002, "B": 100_003, "IR": 100_009},
                "wire_colors_raw_10ns": {"1": 100_001, "2": 100_002, "3": 100_003, "9": 100_009},
            },
        }
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        self.events.append(f"ready-{frame['slot']}")

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        frame = self.job["frames"][self.index]
        ack_path = self.job_path.parent / frame["ack"]
        if not ack_path.exists():
            return None
        ack = json.loads(ack_path.read_text(encoding="utf-8"))
        self.events.append(f"ack-{frame['slot']}-{ack['action']}")
        forced_stop = self.stop_after_index is not None and self.index >= self.stop_after_index
        if ack["action"] == "continue" and not forced_stop and self.index + 1 < len(self.job["frames"]):
            self.index += 1
            self._emit_frame()
            return None
        completed = [item["slot"] for item in self.job["frames"][: self.index + 1]]
        self.session_journal_path.write_text(
            json.dumps(
                {
                    "batch_job_sha256": _sha256(self.job_path.read_bytes()),
                    "capture_bundle_sha256": CAPTURE_BUNDLE_SHA256,
                    "capture_engine_sha256": _sha256(_FAKE_WORKER_SOURCE),
                    "manual_review_approval_sha256_by_slot": {
                        str(item["slot"]): (
                            None if item["manual_review_approval"] is None else item["manual_review_approval"]["binding_sha256"]
                        )
                        for item in self.job["frames"]
                    },
                    "reviewed_roll_fingerprint_sha256": self.job["reviewed_roll_fingerprint"]["binding_sha256"],
                    "completed_slots": completed,
                    "continuation_plan_sha256": CANONICAL_CONTINUATION_PLAN_SHA256,
                    "plan_sha256": CANONICAL_PLAN_SHA256,
                    "recovery_required": "none",
                    "reservation_acquired": True,
                    "selected_slots": [item["slot"] for item in self.job["frames"]],
                    "session_id": self.job["session_id"],
                    "status": "stopped" if ack["action"] == "stop" or forced_stop else "complete",
                    "unit_release_attempts": 1,
                    "unit_released": True,
                }
            ),
            encoding="utf-8",
        )
        self.returncode = 0
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        while self.poll() is None:
            time.sleep(0.001)
        return int(self.returncode)


@dataclass
class _GatedBatchProcess(_FakeBatchProcess):
    """``_FakeBatchProcess`` that withholds every frame after the first
    until released, so streaming-vs-buffering is observable by event order
    rather than by a wall-clock race."""

    release_after_first: threading.Event = field(default_factory=threading.Event)

    def _emit_frame(self) -> None:
        if self.index >= 1:
            self.release_after_first.wait(timeout=5)
        super()._emit_frame()


@dataclass
class _RefusalBatchProcess:
    """RunningBatchProcess double that refuses before any frame is
    captured -- mirrors
    test_terminal_batch_receipt_wakes_parent_before_repeated_process_polling."""

    job_path: Path
    session_journal_path: Path
    message: str

    def __post_init__(self) -> None:
        job = json.loads(self.job_path.read_text(encoding="utf-8"))
        self.session_journal_path.write_text(
            json.dumps(
                {
                    "batch_job_sha256": _sha256(self.job_path.read_bytes()),
                    "capture_bundle_sha256": CAPTURE_BUNDLE_SHA256,
                    "capture_engine_sha256": _sha256(_FAKE_WORKER_SOURCE),
                    "manual_review_approval_sha256_by_slot": {
                        str(item["slot"]): (
                            None if item["manual_review_approval"] is None else item["manual_review_approval"]["binding_sha256"]
                        )
                        for item in job["frames"]
                    },
                    "reviewed_roll_fingerprint_sha256": job["reviewed_roll_fingerprint"]["binding_sha256"],
                    "completed_slots": [],
                    "continuation_plan_sha256": CANONICAL_CONTINUATION_PLAN_SHA256,
                    "plan_sha256": CANONICAL_PLAN_SHA256,
                    "recovery_required": "none",
                    "reservation_acquired": True,
                    "selected_slots": [item["slot"] for item in job["frames"]],
                    "session_id": job["session_id"],
                    "status": "failed",
                    "error": self.message,
                    "finished_unix": 0.0,
                    "unit_release_attempts": 1,
                    "unit_released": True,
                }
            ),
            encoding="utf-8",
        )
        self.returncode = 1

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


_FAKE_WORKER_SOURCE = b"# fake external capture worker\n"


def _adapter(tmp_path: Path, worker: _PreviewAndBatchWorker, *, batch_spawner) -> CaptureProcessAdapter:
    worker_path = tmp_path / "worker.py"
    worker_path.write_bytes(_FAKE_WORKER_SOURCE)
    # expected_worker_sha256 is checked against the real bytes on disk, so it
    # must be derived from them, not chosen independently.
    worker.worker_sha256 = _sha256(_FAKE_WORKER_SOURCE)
    manifest_path = tmp_path / "replay-first-rgbi4-manifest.json"
    manifest_path.write_text(json.dumps({"plan_sha256": CANONICAL_PLAN_SHA256}), encoding="utf-8")
    return CaptureProcessAdapter(
        worker_path=worker_path,
        expected_worker_sha256=worker.worker_sha256,
        manifest_path=manifest_path,
        attempts_root=tmp_path / "attempts",
        python_executable=sys.executable,
        runner=worker,
        batch_spawner=batch_spawner,
    )


def _make_roll(
    tmp_path: Path,
    device: "coolscanpy.Device",
    *,
    material: "coolscanpy.Material" = coolscanpy.Material.COLOR_NEGATIVE,
    batch_spawner,
) -> tuple[Roll, _PreviewAndBatchWorker]:
    worker = _PreviewAndBatchWorker(worker_sha256="")
    adapter = _adapter(tmp_path, worker, batch_spawner=batch_spawner)
    roll = Roll(
        device,
        material,
        adapter=adapter,
        workflow=_make_workflow(),
        attempts_root=tmp_path / "attempts",
    )
    return roll, worker


def _success_spawner(events: list[str], *, stop_after_index: int | None = None):
    def spawn(argv: Sequence[str], *, cwd: Path, stdout: object, stderr: object) -> _FakeBatchProcess:
        del cwd, stdout, stderr
        job_path = Path(_arg(argv, "--batch-job"))
        session_journal_path = Path(_arg(argv, "--session-journal"))
        return _FakeBatchProcess(job_path, session_journal_path, events, stop_after_index=stop_after_index)

    return spawn


def _refusal_spawner(message: str):
    def spawn(argv: Sequence[str], *, cwd: Path, stdout: object, stderr: object) -> _RefusalBatchProcess:
        del cwd, stdout, stderr
        job_path = Path(_arg(argv, "--batch-job"))
        session_journal_path = Path(_arg(argv, "--session-journal"))
        return _RefusalBatchProcess(job_path, session_journal_path, message)

    return spawn


def _gated_spawner(events: list[str], gate: threading.Event):
    def spawn(argv: Sequence[str], *, cwd: Path, stdout: object, stderr: object) -> _GatedBatchProcess:
        del cwd, stdout, stderr
        job_path = Path(_arg(argv, "--batch-job"))
        session_journal_path = Path(_arg(argv, "--session-journal"))
        return _GatedBatchProcess(job_path, session_journal_path, events, release_after_first=gate)

    return spawn


def _open_device(fake_service_factory, **cap_overrides: object) -> "coolscanpy.Device":
    fake_service_factory([_coolscan_device(**cap_overrides)])
    return coolscanpy.open("ls5000")


# ===========================================================================
# get_devices() / open() errors
# ===========================================================================


class TestOpenErrors:
    def test_get_devices_filters_to_coolscan_only(self, fake_service_factory) -> None:
        other = ScannerDevice(id="pieusb:usb:001", vendor="Reflecta", model="Flatbed", capabilities=_caps())
        fake_service_factory([_coolscan_device(), other])

        devices = coolscanpy.get_devices()

        assert [d.id for d in devices] == [_COOLSCAN_ID]

    def test_open_ls5000_alias_with_no_devices_raises_device_not_found(self, fake_service_factory) -> None:
        fake_service_factory([])

        with pytest.raises(coolscanpy.DeviceNotFound):
            coolscanpy.open("ls5000")

    def test_open_ls5000_alias_ambiguous_with_two_devices_raises_device_not_found(self, fake_service_factory) -> None:
        second = ScannerDevice(id="net:scanner:coolscan3:usb:2", vendor="Nikon", model="LS-5000 ED", capabilities=_caps())
        fake_service_factory([_coolscan_device(), second])

        with pytest.raises(coolscanpy.DeviceNotFound):
            coolscanpy.open("ls5000")

    def test_open_exact_id_not_found_raises_device_not_found(self, fake_service_factory) -> None:
        fake_service_factory([_coolscan_device()])

        with pytest.raises(coolscanpy.DeviceNotFound):
            coolscanpy.open("no-such-device")

    def test_open_same_device_twice_raises_device_busy(self, fake_service_factory) -> None:
        fake_service_factory([_coolscan_device()])

        first = coolscanpy.open("ls5000")
        try:
            with pytest.raises(coolscanpy.DeviceBusy):
                coolscanpy.open("ls5000")
        finally:
            first.close()

    def test_close_then_reopen_succeeds(self, fake_service_factory) -> None:
        fake_service_factory([_coolscan_device()])

        first = coolscanpy.open("ls5000")
        first.close()
        second = coolscanpy.open("ls5000")
        second.close()

    def test_device_context_manager_releases_on_exit(self, fake_service_factory) -> None:
        fake_service_factory([_coolscan_device()])

        with coolscanpy.open("ls5000"):
            pass

        # released: opening again does not raise DeviceBusy
        again = coolscanpy.open("ls5000")
        again.close()

    def test_device_close_is_idempotent(self, fake_service_factory) -> None:
        fake_service_factory([_coolscan_device()])

        dev = coolscanpy.open("ls5000")
        dev.close()
        dev.close()  # must not raise


# ===========================================================================
# Device methods refuse to run after close()
# ===========================================================================


class TestDeviceClosedRaises:
    def test_scan_after_close_raises_runtime_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        dev.close()

        with pytest.raises(RuntimeError, match="has been closed"):
            dev.scan()

    def test_roll_after_close_raises_runtime_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        dev.close()

        with pytest.raises(RuntimeError, match="has been closed"):
            dev.roll()

    def test_eject_after_close_raises_runtime_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory, can_eject=True)
        dev.close()

        with pytest.raises(RuntimeError, match="has been closed"):
            dev.eject()


# ===========================================================================
# Device option introspection / get / set
# ===========================================================================


class TestDeviceOptions:
    def test_option_names_is_fixed_sane_shaped_list(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            assert dev.option_names == ["resolution", "depth", "samples", "autofocus", "auto_exposure"]
        finally:
            dev.close()

    def test_getitem_resolution_describes_constraint_from_capabilities(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            option = dev["resolution"]
            assert option.name == "resolution"
            assert option.type == coolscanpy.OptionType.INT
            assert option.unit == coolscanpy.OptionUnit.DPI
            assert option.constraint == (1_000, 2_000, 4_000)
            assert option.active is True
            assert option.settable is True
        finally:
            dev.close()

    def test_getitem_unknown_option_raises_key_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            with pytest.raises(KeyError):
                dev["not-a-real-option"]
        finally:
            dev.close()

    def test_getitem_auto_exposure_reflects_capability_gating(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory, auto_exposure=False)
        try:
            option = dev["auto_exposure"]
            assert option.active is False
            assert option.settable is False
        finally:
            dev.close()

    def test_set_valid_resolution_succeeds(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            dev.resolution = 2_000
            assert dev.resolution == 2_000
        finally:
            dev.close()

    def test_set_invalid_resolution_raises_value_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            with pytest.raises(ValueError):
                dev.resolution = 12_345
        finally:
            dev.close()

    def test_set_samples_without_multi_sample_capability_raises(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory, multi_sample=False)
        try:
            with pytest.raises(ValueError):
                dev.samples = 4
        finally:
            dev.close()

    def test_set_auto_exposure_without_capability_raises(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory, auto_exposure=False)
        try:
            with pytest.raises(ValueError):
                dev.auto_exposure = True
        finally:
            dev.close()

    def test_set_autofocus_wrong_type_raises_type_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            with pytest.raises(TypeError):
                dev.autofocus = "yes"
        finally:
            dev.close()


# ===========================================================================
# Device.scan() / cancel() / eject()
# ===========================================================================


class TestDeviceScanAndEject:
    def test_scan_returns_uint16_hxwx3_array(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            rgb = dev.scan()
            assert rgb.dtype == np.uint16
            assert rgb.ndim == 3 and rgb.shape[2] == 3
        finally:
            dev.close()

    def test_concurrent_scan_raises_device_busy(self, fake_service_factory) -> None:
        backend = fake_service_factory([_coolscan_device()])
        backend.scan_calls  # noqa: B018 - just confirm attribute exists
        dev = coolscanpy.open("ls5000")
        try:
            release = threading.Event()
            started = threading.Event()
            original_scan = dev._service.run_scan

            def blocking_scan(*args, **kwargs):
                started.set()
                release.wait(timeout=2)
                return original_scan(*args, **kwargs)

            dev._service.run_scan = blocking_scan
            worker = threading.Thread(target=dev.scan)
            worker.start()
            started.wait(timeout=2)
            try:
                with pytest.raises(coolscanpy.DeviceBusy):
                    dev.scan()
            finally:
                release.set()
                worker.join(timeout=2)
        finally:
            dev.close()

    def test_eject_returns_false_when_capability_absent(self, fake_service_factory) -> None:
        backend = fake_service_factory([_coolscan_device(can_eject=False)])
        dev = coolscanpy.open("ls5000")
        try:
            assert dev.eject() is False
            assert backend.eject_calls == []
        finally:
            dev.close()

    def test_eject_delegates_to_backend_when_capable(self, fake_service_factory) -> None:
        backend = fake_service_factory([_coolscan_device(can_eject=True)])
        dev = coolscanpy.open("ls5000")
        try:
            assert dev.eject() is True
            assert backend.eject_calls == [_COOLSCAN_ID]
        finally:
            dev.close()

    def test_eject_failure_raises_eject_failed(self, fake_service_factory) -> None:
        backend = fake_service_factory([_coolscan_device(can_eject=True)])
        backend.eject_error = RuntimeError("transport hiccup")
        dev = coolscanpy.open("ls5000")
        try:
            with pytest.raises(coolscanpy.EjectFailed):
                dev.eject()
        finally:
            dev.close()


# ===========================================================================
# Roll reservation lifecycle (Device.roll())
# ===========================================================================


class TestRollReservationLifecycle:
    def test_roll_without_adapter_capability_raises_value_error(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory, adapter_frame_capacity=None, adapter_frame_control=False)
        try:
            with pytest.raises(ValueError):
                dev.roll()
        finally:
            dev.close()

    def test_second_roll_while_first_open_raises_device_busy(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        try:
            first = dev.roll()
            try:
                with pytest.raises(coolscanpy.DeviceBusy):
                    dev.roll()
            finally:
                first.close()
        finally:
            dev.close()

    def test_roll_context_manager_releases_reservation_exactly_once(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            with dev.roll() as roll:
                assert isinstance(roll, Roll)
            # released: a second roll() succeeds immediately after
            second = dev.roll()
            second.close()
        finally:
            dev.close()

    def test_roll_close_is_idempotent(self, fake_service_factory) -> None:
        dev = _open_device(fake_service_factory)
        try:
            roll = dev.roll()
            roll.close()
            roll.close()  # must not raise
            # and the reservation really was released
            again = dev.roll()
            again.close()
        finally:
            dev.close()


# ===========================================================================
# Roll.preview() / spacing offset / approval / fingerprint
# ===========================================================================


class TestRollPreview:
    def test_preview_returns_forty_thumbnails(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            thumbnails = roll.preview()
            assert len(thumbnails) == 40
            assert [t.slot for t in thumbnails] == list(range(1, 41))
            assert all(t.image.dtype == np.uint16 for t in thumbnails)
            assert all(t.image.shape[1:] == (96, 3) for t in thumbnails)
        finally:
            roll.close()
            dev.close()

    def test_preview_filters_returned_thumbnails_by_requested_slots(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            thumbnails = roll.preview([3, 7, 19])
            assert [t.slot for t in thumbnails] == [3, 7, 19]
        finally:
            roll.close()
            dev.close()

    def test_fingerprint_before_preview_raises_runtime_error(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            with pytest.raises(RuntimeError):
                roll.fingerprint  # noqa: B018
        finally:
            roll.close()
            dev.close()

    def test_fingerprint_after_preview_has_slot_count_and_sha256(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            fp = roll.fingerprint
            assert fp.slot_count == 40
            assert len(fp.sha256) == 64
            assert fp.preview_shape[2] == 3
        finally:
            roll.close()
            dev.close()

    def test_spacing_offset_defaults_to_zero_and_can_be_set(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            assert roll.spacing_offset(10) == 0
            roll.set_spacing_offset(10, 5)
            assert roll.spacing_offset(10) == 5
        finally:
            roll.close()
            dev.close()

    def test_spacing_offset_out_of_range_raises_value_error(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            with pytest.raises(ValueError):
                # slot 1's range is [0, 144] (never negative); every other
                # slot allows [-144, 144].
                roll.set_spacing_offset(1, -5)
        finally:
            roll.close()
            dev.close()

    def test_unknown_slot_raises_value_error(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            with pytest.raises(ValueError):
                roll.spacing_offset(999)
        finally:
            roll.close()
            dev.close()

    def test_set_spacing_offset_invalidates_prior_approval(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            slot = next(t.slot for t in roll.preview() if t.needs_approval) if any(
                roll.needs_approval(s) for s in range(1, 41)
            ) else None
            if slot is None:
                pytest.skip("synthetic preview produced no manual-review slot to test invalidation against")
            roll.approve(slot)
            assert slot in roll._approvals
            roll.set_spacing_offset(slot, 1 if slot != 1 else 0)
            assert slot not in roll._approvals
        finally:
            roll.close()
            dev.close()

    def test_approve_on_slot_that_does_not_need_approval_raises_value_error(
        self,
        fake_service_factory,
        tmp_path: Path,
    ) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            clean_slot = next((s for s in range(1, 41) if not roll.needs_approval(s)), None)
            if clean_slot is None:
                pytest.skip("synthetic preview flagged every slot for manual review")
            with pytest.raises(ValueError):
                roll.approve(clean_slot)
        finally:
            roll.close()
            dev.close()


# ===========================================================================
# Roll.scan_many() / scan()
# ===========================================================================


class TestRollScanMany:
    def test_scan_many_yields_frames_in_requested_order(self, fake_service_factory, tmp_path: Path) -> None:
        events: list[str] = []
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner(events))
        try:
            roll.preview()
            # CaptureBatchRequest requires unique, strictly increasing slots
            # (one continuous forward transport reservation) -- "ordering"
            # here means the yielded Frames replay that same sequence, not
            # that arbitrary reordering is supported.
            for slot in (2, 5, 9):
                if roll.needs_approval(slot):
                    roll.approve(slot)

            frames = list(roll.scan_many([2, 5, 9]))

            assert [frame.slot for frame in frames] == [2, 5, 9]
            for frame in frames:
                assert frame.rgb.dtype == np.uint16
                assert frame.ir.dtype == np.uint16
                assert frame.ir_validity.dtype == np.bool_
                assert frame.receipt.slot == frame.slot
                assert frame.receipt.dpi == 4_000
                assert frame.receipt.depth == 16
                assert frame.receipt.device_id == _COOLSCAN_ID
                assert frame.receipt.reviewed_fingerprint_sha256 == roll.fingerprint.sha256
                assert frame.receipt.fresh_fingerprint_sha256 == roll.fingerprint.sha256
                assert frame.receipt.transport_smear.verdict == "clean"
                assert frame.receipt.exposure.red_exposure_us == pytest.approx(1_000.01)
                assert frame.receipt.split_alignment is None
                assert "rgb" in frame.receipt.artifacts and "ir" in frame.receipt.artifacts
        finally:
            roll.close()
            dev.close()

    def test_scan_is_sugar_for_scan_many_of_one(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            roll.preview()
            if roll.needs_approval(11):
                roll.approve(11)

            frame = roll.scan(11)

            assert frame.slot == 11
        finally:
            roll.close()
            dev.close()

    def test_scan_many_raises_manual_review_required_when_unapproved(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            thumbnails = roll.preview()
            flagged = next((t.slot for t in thumbnails if t.needs_approval), None)
            if flagged is None:
                pytest.skip("synthetic preview produced no manual-review slot")

            with pytest.raises(coolscanpy.ManualReviewRequired) as excinfo:
                next(iter(roll.scan_many([flagged])))

            assert excinfo.value.slot == flagged
        finally:
            roll.close()
            dev.close()

    def test_scan_many_black_and_white_route_not_implemented(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(
            tmp_path,
            dev,
            material=coolscanpy.Material.BLACK_AND_WHITE_NEGATIVE,
            batch_spawner=_success_spawner([]),
        )
        try:
            roll.preview()
            with pytest.raises(NotImplementedError):
                next(iter(roll.scan_many([1])))
        finally:
            roll.close()
            dev.close()

    def test_safe_stop_mid_batch_yields_completed_frames_then_raises(self, fake_service_factory, tmp_path: Path) -> None:
        events: list[str] = []
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner(events))
        try:
            roll.preview()
            for slot in (1, 2, 3):
                if roll.needs_approval(slot):
                    roll.approve(slot)

            seen: list[int] = []

            def on_progress(progress: coolscanpy.Progress) -> None:
                seen.append(progress.slot)
                if len(seen) == 1:
                    roll.safe_stop()

            iterator = roll.scan_many([1, 2, 3], on_progress=on_progress)
            produced = []
            with pytest.raises(coolscanpy.SafeStopRequested):
                for frame in iterator:
                    produced.append(frame)

            assert [frame.slot for frame in produced] == [1]
        finally:
            roll.close()
            dev.close()

    def test_scan_many_yields_first_frame_before_batch_completes(self, fake_service_factory, tmp_path: Path) -> None:
        events: list[str] = []
        gate = threading.Event()
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_gated_spawner(events, gate))
        try:
            roll.preview()
            for slot in (2, 5, 9):
                if roll.needs_approval(slot):
                    roll.approve(slot)

            iterator = roll.scan_many([2, 5, 9])
            first = next(iterator)

            assert first.slot == 2
            # The second requested frame's journal is gated until we release
            # it below. Seeing it absent here -- deterministically, not by
            # timing -- proves the first frame reached the caller without
            # the rest of the batch having run yet, i.e. streaming rather
            # than buffer-then-yield.
            assert not any(event.startswith("ready-5") for event in events)

            gate.set()
            rest = list(iterator)

            assert [frame.slot for frame in rest] == [5, 9]
        finally:
            roll.close()
            dev.close()

    def test_scan_many_early_close_requests_safe_stop_without_deadlock(
        self, fake_service_factory, tmp_path: Path
    ) -> None:
        events: list[str] = []
        dev = _open_device(fake_service_factory)
        # Two slots, not three: safe_stop() never aborts a frame already in
        # flight (only the *next* one is refused), so abandoning after the
        # first slot still lets one more full frame -- with this fixture's
        # real per-frame hashing/TIFF I/O -- finish before the batch winds
        # down. Keeping the batch short bounds that unavoidable tail cost.
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner(events))
        try:
            roll.preview()
            for slot in (2, 5):
                if roll.needs_approval(slot):
                    roll.approve(slot)

            iterator = roll.scan_many([2, 5])
            first = next(iterator)
            assert first.slot == 2

            # Abandon mid-batch on a watchdog thread: if the worker were left
            # blocked writing a completed frame to a now-unread, full queue,
            # close() would hang and this join would time out. The bound is
            # generous (not tight) because it must comfortably outlast the
            # in-flight frame's real processing, not just prove promptness.
            closer = threading.Thread(target=iterator.close)
            closer.start()
            closer.join(timeout=90.0)

            assert not closer.is_alive(), "generator.close() deadlocked instead of returning"
            assert roll._stop_event.is_set()
        finally:
            roll.close()
            dev.close()

    def test_scan_many_worker_exception_surfaces_with_original_type_after_earlier_frames(
        self, fake_service_factory, tmp_path: Path
    ) -> None:
        events: list[str] = []
        dev = _open_device(fake_service_factory)
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner(events))

        calls = 0

        def flaky_smear_assessor(rgb, *, dpi):
            nonlocal calls
            calls += 1
            if calls == 2:
                return StoppedTransportSmearAssessment(
                    verdict="smear",
                    start_row=10,
                    suffix_rows=5,
                    minimum_matches=64,
                    tail_median_rms=1.0,
                    tail_min_corr=0.1,
                    pre_tail_median_rms=1.0,
                    texture_span=1.0,
                    reason="synthetic smear for test",
                )
            return StoppedTransportSmearAssessment(
                verdict="clean",
                start_row=None,
                suffix_rows=0,
                minimum_matches=64,
                tail_median_rms=None,
                tail_min_corr=None,
                pre_tail_median_rms=None,
                texture_span=1_234.0,
                reason="unit-test clean",
            )

        # Swap in a workflow whose smear QC refuses the SECOND finalized
        # frame, so the worker thread raises mid-batch -- the first frame
        # must already be in the caller's hands with the right type
        # (TransportSmearDetected) surfacing for the second.
        roll._workflow = LS5000SinglePassWorkflow(
            contract=PackedCaptureContract(),
            decoder=lambda _path: (
                _fake_decoded_frame(),
                {
                    "padding_validated_records": CANONICAL_FINE_READ_COUNT,
                    "rgb_samples_decoded": 4,
                    "ir_planes_transferred": 1,
                },
            ),
            smear_assessor=flaky_smear_assessor,
        )
        try:
            roll.preview()
            for slot in (2, 5):
                if roll.needs_approval(slot):
                    roll.approve(slot)

            produced = []
            with pytest.raises(coolscanpy.TransportSmearDetected):
                for frame in roll.scan_many([2, 5]):
                    produced.append(frame)

            assert [frame.slot for frame in produced] == [2]
        finally:
            roll.close()
            dev.close()


# ===========================================================================
# Roll batch refusal -> typed exceptions
# ===========================================================================


class TestRollBatchRefusal:
    def test_fingerprint_mismatch_message_raises_fingerprint_refused(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        message = (
            "SynchronizedProtocolError: fresh live index does not match the "
            "reviewed roll fingerprint: visual-content-mismatch"
        )
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_refusal_spawner(message))
        try:
            roll.preview()
            if roll.needs_approval(1):
                roll.approve(1)
            with pytest.raises(coolscanpy.FingerprintRefused) as excinfo:
                next(iter(roll.scan_many([1])))
            assert excinfo.value.comparison.matches is False
        finally:
            roll.close()
            dev.close()

    def test_manual_review_message_raises_manual_review_required(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        message = "SynchronizedProtocolError: frame 4 transport origin requires manual review; approve its current thumbnail"
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_refusal_spawner(message))
        try:
            roll.preview()
            if roll.needs_approval(4):
                roll.approve(4)
            with pytest.raises(coolscanpy.ManualReviewRequired) as excinfo:
                next(iter(roll.scan_many([4])))
            assert excinfo.value.slot == 4
        finally:
            roll.close()
            dev.close()

    def test_other_refusal_message_raises_generic_roll_mismatch(self, fake_service_factory, tmp_path: Path) -> None:
        dev = _open_device(fake_service_factory)
        message = "SynchronizedProtocolError: metering refused"
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_refusal_spawner(message))
        try:
            roll.preview()
            if roll.needs_approval(1):
                roll.approve(1)
            with pytest.raises(coolscanpy.RollMismatch) as excinfo:
                next(iter(roll.scan_many([1])))
            assert not isinstance(excinfo.value, (coolscanpy.FingerprintRefused, coolscanpy.ManualReviewRequired))
        finally:
            roll.close()
            dev.close()


# ===========================================================================
# SANE-free fallback: get_devices()/open()/scan()/eject()/roll() when
# python-sane is not importable (see coolscanpy._device's module docstring)
# ===========================================================================


@dataclass
class _FakeUsbDevice:
    """Just enough of a ``usb.core.Device`` for ``_usb_fallback_device_infos``
    to read: the two attributes it actually uses."""

    bus: int
    address: int


@pytest.fixture
def python_sane_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates python-sane genuinely not being installed, as opposed to
    installed-but-misbehaving (which tests/session/test_service.py already
    covers with a fake module object standing in for ``sane``). Setting the
    ``sys.modules`` entry to ``None`` makes any ``import sane`` -- anywhere,
    including inside ``SaneBackend.__init__`` -- raise ``ImportError``,
    exactly like an environment that never had python-sane installed. This
    exercises the real ``ScannerService``/``SaneBackend`` classes, not a
    hand-rolled stand-in for them."""

    monkeypatch.setitem(sys.modules, "sane", None)


def _mock_usb_find(monkeypatch: pytest.MonkeyPatch, devices: list[_FakeUsbDevice]) -> None:
    """Stands in for ``usb.core.find`` so the fallback enumeration path never
    touches a real USB bus."""

    monkeypatch.setattr("usb.core.find", lambda find_all=False, **kwargs: list(devices))


class TestSaneFreeFallback:
    def test_get_devices_falls_back_to_direct_usb_when_device_present(
        self, python_sane_unavailable: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_usb_find(monkeypatch, [_FakeUsbDevice(bus=1, address=7)])

        devices = coolscanpy.get_devices()

        assert len(devices) == 1
        info = devices[0]
        assert info.id == "usb:1:7"
        assert info.vendor == "Nikon"
        assert info.model == "LS-5000 ED"
        assert info.capabilities.adapter_frame_control is True

    def test_get_devices_falls_back_to_empty_list_when_no_usb_device(
        self, python_sane_unavailable: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_usb_find(monkeypatch, [])

        assert coolscanpy.get_devices() == []

    def test_open_succeeds_without_python_sane(
        self, python_sane_unavailable: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_usb_find(monkeypatch, [_FakeUsbDevice(bus=1, address=7)])

        dev = coolscanpy.open("ls5000")
        try:
            assert dev.capabilities.adapter_frame_control is True
        finally:
            dev.close()

    def test_plain_scan_without_python_sane_raises_import_error_with_install_hint(
        self, python_sane_unavailable: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_usb_find(monkeypatch, [_FakeUsbDevice(bus=1, address=7)])
        dev = coolscanpy.open("ls5000")
        try:
            with pytest.raises(ImportError, match=r'pip install "coolscanpy\[scanner\]"'):
                dev.scan()
        finally:
            dev.close()

    def test_eject_without_python_sane_raises_import_error(
        self, python_sane_unavailable: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_usb_find(monkeypatch, [_FakeUsbDevice(bus=1, address=7)])
        dev = coolscanpy.open("ls5000")
        try:
            with pytest.raises(ImportError, match="python-sane not importable"):
                dev.eject()
        finally:
            dev.close()

    def test_roll_is_reachable_without_python_sane(
        self,
        python_sane_unavailable: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The roll-feeder extension never uses SANE (see the module
        docstrings across coolscanpy._roll/roll/protocol/capture): a Device
        opened through the pyusb fallback must be able to preview and
        fine-scan a roll exactly like a SANE-backed one, using the same
        injectable adapter/workflow doubles as every other roll test above
        -- still no real hardware, and now no python-sane either."""

        _mock_usb_find(monkeypatch, [_FakeUsbDevice(bus=1, address=7)])
        dev = coolscanpy.open("ls5000")
        roll, _worker = _make_roll(tmp_path, dev, batch_spawner=_success_spawner([]))
        try:
            thumbnails = roll.preview()
            assert len(thumbnails) == 40

            slot = 5
            if roll.needs_approval(slot):
                roll.approve(slot)
            frame = roll.scan(slot)

            assert frame.slot == slot
            assert frame.receipt.device_id == dev._info.id
        finally:
            roll.close()
            dev.close()


# ===========================================================================
# Exception hierarchy
# ===========================================================================


class TestExceptionHierarchy:
    def test_all_exceptions_derive_from_root(self) -> None:
        for name in (
            "DeviceNotFound",
            "DeviceBusy",
            "EjectFailed",
            "SafeStopRequested",
            "FeederParked",
            "RollMismatch",
            "FingerprintRefused",
            "ManualReviewRequired",
            "GeometryValidationError",
            "TransportSmearDetected",
            "SplitAlignmentError",
            "BatchIntegrityError",
        ):
            exc_type = getattr(coolscanpy, name)
            assert issubclass(exc_type, coolscanpy.PyCoolscanError)

    def test_fingerprint_refused_and_manual_review_required_are_roll_mismatch(self) -> None:
        assert issubclass(coolscanpy.FingerprintRefused, coolscanpy.RollMismatch)
        assert issubclass(coolscanpy.ManualReviewRequired, coolscanpy.RollMismatch)

    def test_fingerprint_refused_carries_comparison(self) -> None:
        comparison = coolscanpy.FingerprintComparison(
            matches=False,
            reason="test",
            compared_frames=1,
            visual_median_hamming=None,
            visual_p90_hamming=None,
            frame_start_median_delta_rows=None,
            frame_start_max_delta_rows=None,
        )
        error = coolscanpy.FingerprintRefused("nope", comparison=comparison)
        assert error.comparison is comparison

    def test_manual_review_required_carries_slot(self) -> None:
        error = coolscanpy.ManualReviewRequired("nope", slot=7)
        assert error.slot == 7
