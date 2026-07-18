"""Public dataclasses and enums shared across the top-level surface.

``Material`` is a re-export of the concrete package's own
:class:`coolscanpy.roll.controls.ScanMaterial` under the public name used by
the API contract -- the recipe each member maps to (dpi/depth/samples/IR) is
computed by the concrete ``recipe_for_material`` function, not duplicated
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Literal

import numpy as np

from coolscanpy.roll.controls import ScanMaterial as Material

__all__ = [
    "Material",
    "OptionType",
    "OptionUnit",
    "Option",
    "Capabilities",
    "DeviceInfo",
    "Thumbnail",
    "RollFingerprint",
    "FingerprintComparison",
    "Progress",
    "ProgressCallback",
    "ExposureVector",
    "SplitAlignment",
    "ClippingTelemetry",
    "FocusDetailTelemetry",
    "TransportSmearAssessment",
    "ArtifactEvidence",
    "ApprovalReceipt",
    "Receipt",
    "Frame",
]


class OptionType(StrEnum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"


class OptionUnit(StrEnum):
    NONE = "none"
    PIXEL = "pixel"
    DPI = "dpi"
    MICROSECOND = "microsecond"


@dataclass(frozen=True)
class Option:
    """Mirrors python-sane's ``Option`` (name/title/desc/type/unit/constraint
    plus ``is_active()``/``is_settable()``), trimmed of SANE-internal
    bookkeeping that has no meaning outside a SANE backend."""

    name: str
    title: str
    desc: str
    type: OptionType
    unit: OptionUnit
    constraint: tuple[float, float, float] | tuple[int, ...] | tuple[str, ...] | None
    active: bool
    settable: bool


@dataclass(frozen=True)
class Capabilities:
    ir_channel: bool
    supported_dpi: tuple[int, ...]
    supported_depths: tuple[int, ...]
    multi_sample: bool
    adapter_frame_capacity: int | None
    adapter_frame_control: bool
    auto_exposure: bool
    registered_geometry: bool
    can_eject: bool


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    vendor: str
    model: str
    capabilities: Capabilities


@dataclass(frozen=True)
class Thumbnail:
    slot: int
    image: np.ndarray
    boundary_rows: tuple[int, int]
    spacing_offset: int
    needs_approval: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RollFingerprint:
    """Opaque physical-roll identity bound at the last ``preview()``."""

    sha256: str
    slot_count: int
    preview_shape: tuple[int, int, int]


@dataclass(frozen=True)
class FingerprintComparison:
    """Attached to :class:`coolscanpy.exceptions.FingerprintRefused` when a
    fresh transport read disagrees with the reviewed fingerprint."""

    matches: bool
    reason: str
    compared_frames: int
    visual_median_hamming: float | None
    visual_p90_hamming: int | None
    frame_start_median_delta_rows: float | None
    frame_start_max_delta_rows: int | None


@dataclass(frozen=True)
class Progress:
    stage: Literal["preview", "fine-scan"]
    slot: int | None
    index: int
    total: int
    fraction: float
    message: str


ProgressCallback = Callable[[Progress], None]


@dataclass(frozen=True)
class ExposureVector:
    """The replayable per-channel state the scanner actually used."""

    focus_position: int
    exposure_multiplier: float
    red_exposure_us: float
    green_exposure_us: float
    blue_exposure_us: float


@dataclass(frozen=True)
class SplitAlignment:
    """How IR was registered to the multisampled RGB in a split Coolscan
    capture, with full audit confidence."""

    mode: str
    dx_px: float
    dy_px: float
    phase_responses: tuple[float, ...]
    channel_spread_px: float | None
    ecc_coefficient: float | None
    tile_support_counts: tuple[int, ...]
    tile_shift_spread_px: float | None
    estimator_version: int | None
    multiscale_max_dimensions: tuple[int, ...]
    multiscale_channel_shifts_px: tuple[tuple[tuple[float, float], ...], ...]
    multiscale_responses: tuple[tuple[float, ...], ...]
    multiscale_tile_support_counts: tuple[tuple[int, ...], ...]
    multiscale_tile_shift_spreads_px: tuple[tuple[float, ...], ...]
    multiscale_global_alias_shifts_px: tuple[tuple[tuple[float, float], ...], ...]


@dataclass(frozen=True)
class ClippingTelemetry:
    """Warning-only. Never gates a capture."""

    fractions: tuple[float, float, float]
    clip_level: float
    warning_fraction: float
    warning: bool


@dataclass(frozen=True)
class FocusDetailTelemetry:
    """Scene-dependent detail proxy; informational, never an autofocus
    gate."""

    method: str
    verdict: Literal["measured", "indeterminate"]
    score: float | None
    texture_span: float


@dataclass(frozen=True)
class TransportSmearAssessment:
    """Fail-closed assessment of an abnormally repeated RGB tail (a stopped
    transport re-reading the same rows)."""

    verdict: Literal["clean", "smear", "indeterminate"]
    start_row: int | None
    suffix_rows: int
    minimum_matches: int
    tail_median_rms: float | None
    tail_min_corr: float | None
    pre_tail_median_rms: float | None
    texture_span: float | None
    reason: str


@dataclass(frozen=True)
class ArtifactEvidence:
    """In-memory sha256/shape/dtype of a returned array."""

    sha256: str
    byte_length: int
    shape: tuple[int, ...]
    dtype: str


@dataclass(frozen=True)
class ApprovalReceipt:
    reviewed_fingerprint_sha256: str
    slot: int
    spacing_offset: int
    thumbnail_sha256: str
    reviewed_lookup_row: int
    reviewed_native_origin: int
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Receipt:
    """One receipt per :class:`Frame`, entirely in-memory."""

    version: int
    slot: int
    spacing_offset: int
    dpi: int
    depth: int
    device_id: str
    device_model: str
    reviewed_fingerprint_sha256: str
    fresh_fingerprint_sha256: str
    manual_approval: ApprovalReceipt | None
    exposure: ExposureVector
    split_alignment: SplitAlignment | None
    clipping: ClippingTelemetry
    focus_detail: FocusDetailTelemetry
    transport_smear: TransportSmearAssessment
    artifacts: dict[str, ArtifactEvidence]


@dataclass(frozen=True)
class Frame:
    slot: int
    rgb: np.ndarray
    ir: np.ndarray | None
    ir_validity: np.ndarray | None
    receipt: Receipt
