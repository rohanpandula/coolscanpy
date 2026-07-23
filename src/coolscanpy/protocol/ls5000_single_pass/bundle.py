"""Integrity identity for the package-owned LS-5000 capture bundle."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from .continuation_plan import (
    CANONICAL_CONTINUATION_PLAN_FILENAME,
    CANONICAL_CONTINUATION_PLAN_SHA256,
    canonical_continuation_plan_bytes,
)
from .plan import CANONICAL_PLAN_SHA256, canonical_plan_bytes


DATA_PACKAGE = "coolscanpy.protocol.ls5000_single_pass.data"
CANONICAL_MANIFEST_FILENAME = "replay-first-rgbi4-manifest.json"

# These hashes bind the scanner-facing implementation and both wire resources.
# Update them only after the corresponding hardware-free regression suite passes.
# ``packed.py`` (the shared decode kernel and streaming decoder) and
# ``streaming_sidecar.py`` (the fail-open capture hook) are pinned because the
# capture worker imports them at runtime, so their bytes are part of the
# scanner-facing capture identity. ``density.py`` is pinned for the same
# reason: it validates the acquisition-specific READ(0x8c) replies, the
# density-source cap-0x10d/f03 exposures, the proven 97-dpi reservation-preview
# evidence, runtime arithmetic gate, and exact per-frame ownership receipt.
CAPTURE_BUNDLE_COMPONENT_SHA256 = {
    "capture_process.py": "d4addacf8ddf45ef30d50cfc86b805aa5cefbd5e8050f58b09079ec6da654c8f",
    "worker.py": "5d93982f85fc9899d77be5797544343cf7b84ba166dedfb0ae1c2864b5e90dd7",
    "usb_backend.py": "a1cee3db705afa0067e5866b3efe520c743b09f444f2b8024b72b84ac0cd6932",
    "density.py": "6b2ffcf5ad65b6c48e0be0ce94d18625845fcff6398ef2f818c7b9be9bedbb80",
    "packed.py": "aae6707216d8ed50c12dc7859b78c7331150fd3cde074fb9bf9c60938dd3604f",
    "streaming_sidecar.py": "81ca79a72b37dee579d57be07bd00f59f6e7843a43710bab1811d8b9a94dffb7",
    "continuation_plan.py": "bfdebfaa28075c708f3e8ef070083edce36a28b497bba622173cbb6d1466a282",
    "meter.py": "c7d00c9c8796b7264a553848106a1fe075ab4a25315fbe5a05d05bc35515ca10",
    "roll_index.py": "c7c62775452cf31e1843e551f939a59446be3751a7e8765ebc12c68d44b6aa63",
    "window.py": "5edd64a2f55cb3c968bb380d548d0d9002b41b26f5f4713e5d9b889910d5ed4f",
    "data/replay-first-rgbi4-plan.jsonl": CANONICAL_PLAN_SHA256,
    f"data/{CANONICAL_CONTINUATION_PLAN_FILENAME}": (
        CANONICAL_CONTINUATION_PLAN_SHA256
    ),
    "data/replay-first-rgbi4-manifest.json": "a87faad5aa4cb458d6044cc218ab4fc13ce84f03d5355e7ea47c04c76f290e5f",
}
CAPTURE_WORKER_SHA256 = CAPTURE_BUNDLE_COMPONENT_SHA256["worker.py"]
CAPTURE_BUNDLE_SHA256 = hashlib.sha256(
    json.dumps(
        CAPTURE_BUNDLE_COMPONENT_SHA256,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
).hexdigest()


class CaptureBundleIntegrityError(RuntimeError):
    """The installed scanner capture bundle differs from its pinned identity."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_manifest_bytes() -> bytes:
    """Return the bundled Nikon wire manifest after validating its plan binding."""

    payload = files(DATA_PACKAGE).joinpath(CANONICAL_MANIFEST_FILENAME).read_bytes()
    expected = CAPTURE_BUNDLE_COMPONENT_SHA256[f"data/{CANONICAL_MANIFEST_FILENAME}"]
    actual = _sha256(payload)
    if actual != expected:
        raise CaptureBundleIntegrityError(
            f"canonical capture manifest SHA-256 mismatch: expected {expected}, got {actual}"
        )
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureBundleIntegrityError(
            f"canonical capture manifest is not valid JSON: {error}"
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("plan_sha256") != CANONICAL_PLAN_SHA256
    ):
        raise CaptureBundleIntegrityError(
            "canonical capture manifest is not bound to the packaged replay plan"
        )
    return payload


def verify_capture_bundle(*, require_python_sources: bool) -> str:
    """Verify package resources and, when available, every Python source file.

    Installed wheels and source checkouts expose their ``.py`` files directly.
    A frozen app executes modules from PyInstaller's signed archive, so it can
    only revalidate the separately bundled wire resources at runtime; its code
    identity is the app bundle signature plus ``CAPTURE_BUNDLE_SHA256``.
    """

    if _sha256(canonical_plan_bytes()) != CANONICAL_PLAN_SHA256:
        raise CaptureBundleIntegrityError("canonical capture plan changed")
    if (
        _sha256(canonical_continuation_plan_bytes())
        != CANONICAL_CONTINUATION_PLAN_SHA256
    ):
        raise CaptureBundleIntegrityError("canonical continuation plan changed")
    canonical_manifest_bytes()
    if require_python_sources:
        package_root = Path(__file__).resolve().parent
        for relative, expected in CAPTURE_BUNDLE_COMPONENT_SHA256.items():
            if relative.startswith("data/"):
                continue
            path = package_root / relative
            if not path.is_file():
                raise CaptureBundleIntegrityError(
                    f"capture component is not a regular file: {path}"
                )
            actual = _sha256(path.read_bytes())
            if actual != expected:
                raise CaptureBundleIntegrityError(
                    f"capture component {relative} SHA-256 mismatch: "
                    f"expected {expected}, got {actual}"
                )
    return CAPTURE_BUNDLE_SHA256


__all__ = [
    "CANONICAL_MANIFEST_FILENAME",
    "CAPTURE_BUNDLE_COMPONENT_SHA256",
    "CAPTURE_BUNDLE_SHA256",
    "CAPTURE_WORKER_SHA256",
    "CaptureBundleIntegrityError",
    "canonical_manifest_bytes",
    "verify_capture_bundle",
]
