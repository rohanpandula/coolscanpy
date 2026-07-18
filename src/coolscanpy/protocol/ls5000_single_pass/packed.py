"""Decode Nikon LS-5000 single-pass RGBI4x fine records.

Both inputs are supported:

* 65,508-byte USBPcap prefixes (2,980 records), and
* complete 207,872-byte live responses (2,980 records).

The full live response contains four distinct RGB samples and one transferred
IR plane.  RGB is averaged round-half-up; IR is emitted unchanged.  Prefix
captures contain only the first RGB sample plus IR and remain supported for
wire/reference validation.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


WIDTH = 3_946
HEIGHT = 5_959
CHANNELS = 4
RGB_CHANNELS = 3
ROWS_PER_RECORD = 2
EXPECTED_RECORDS = (HEIGHT + 1) // 2
PREFIX_RECORD_BYTES = 65_508
FULL_RECORD_BYTES = 207_872
CORE_WORDS = WIDTH * CHANNELS * ROWS_PER_RECORD
CORE_BYTES = CORE_WORDS * 2
UNIT_WORDS = WIDTH * ROWS_PER_RECORD
UNIT_BYTES = UNIT_WORDS * 2
FULL_RECORD_WORDS = FULL_RECORD_BYTES // 2

# Half-open byte offsets established by the complete run-10 capture.
FULL_RGB_SAMPLE_BYTE_OFFSETS = (
    (0, 15_784, 31_568),
    (63_488, 79_272, 95_056),
    (111_616, 127_400, 143_184),
    (159_744, 175_528, 191_312),
)
FULL_IR_BYTE_OFFSET = 47_352
FULL_PADDING_BYTE_RANGES = (
    (63_136, 63_488),
    (110_840, 111_616),
    (158_968, 159_744),
    (207_096, 207_872),
)


def infer_record_geometry(path: Path, records: int = EXPECTED_RECORDS) -> int:
    size = path.stat().st_size
    if size % records:
        raise ValueError(f"stream size {size} is not divisible by {records} records")
    record_bytes = size // records
    if record_bytes not in (PREFIX_RECORD_BYTES, FULL_RECORD_BYTES):
        raise ValueError(f"record size {record_bytes} is neither {PREFIX_RECORD_BYTES} nor {FULL_RECORD_BYTES}")
    return record_bytes


def decode_core_records(
    path: Path,
    *,
    record_bytes: int,
    width: int = WIDTH,
    height: int = HEIGHT,
    channels: int = CHANNELS,
) -> np.ndarray:
    records = (height + 1) // 2
    expected_size = records * record_bytes
    if path.stat().st_size != expected_size:
        raise ValueError(f"stream has {path.stat().st_size} bytes, expected {expected_size}")
    core_words = width * channels * ROWS_PER_RECORD
    core_bytes = core_words * 2
    if record_bytes < core_bytes:
        raise ValueError(f"record {record_bytes} is shorter than core {core_bytes}")

    raw = np.memmap(path, dtype=np.uint8, mode="r", shape=(records, record_bytes))
    words = raw[:, :core_bytes].view(">u2")
    paired = words.reshape(records, channels, width, ROWS_PER_RECORD)
    rows = paired.transpose(0, 3, 2, 1).reshape(-1, width, channels)
    # The clean oracle exposes 5,960 row slots for a requested 5,959 rows.
    # Reverse X to the driver's native film orientation.
    return np.asarray(rows[:height, ::-1, :]).copy()


def _counter_train_ok(
    block: np.ndarray,
    *,
    first_counter: int,
) -> bool:
    if block.ndim != 2 or block.shape[1] % 2:
        return False
    expected = (first_counter + np.arange(block.shape[1] // 2, dtype=np.uint32)) & 0xFFFF
    return bool(np.all(block[:, 0::2] == 0xAA55) and np.all(block[:, 1::2] == expected.astype(np.uint16)[None, :]))


def validate_full_record_layout(words: np.ndarray) -> dict[str, object]:
    """Fail closed on the four invariant padding regions in every record."""
    if words.ndim != 2 or words.shape[1] != FULL_RECORD_WORDS:
        raise ValueError(f"unexpected full-record word shape {words.shape}")

    pad0 = words[:, 63_136 // 2 : 63_488 // 2]
    pad1 = words[:, 110_840 // 2 : 111_616 // 2]
    pad2 = words[:, 158_968 // 2 : 159_744 // 2]
    pad3 = words[:, 207_096 // 2 : 207_872 // 2]
    ir_head = words[:, FULL_IR_BYTE_OFFSET // 2 : FULL_IR_BYTE_OFFSET // 2 + 388]

    if not _counter_train_ok(pad0, first_counter=0xE7FD):
        raise ValueError("full-record padding 0 counter train mismatch")
    if not _counter_train_ok(pad1, first_counter=0xD893):
        raise ValueError("full-record padding 1 counter train mismatch")
    if not np.array_equal(pad2, ir_head):
        raise ValueError("full-record padding 2 is not the expected IR-head copy")
    if not _counter_train_ok(pad3, first_counter=0xD893):
        raise ValueError("full-record padding 3 counter train mismatch")

    return {
        "padding_validated_records": int(words.shape[0]),
        "padding_byte_ranges": [list(bounds) for bounds in FULL_PADDING_BYTE_RANGES],
        "padding_2_semantics": "duplicate of first 388 IR words; discarded",
    }


def decode_full_records(
    path: Path,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    validate_padding: bool = True,
    chunk_records: int = 64,
) -> tuple[np.ndarray, dict[str, object]]:
    """Average four RGB samples and extract the single transferred IR plane."""
    if width != WIDTH:
        raise ValueError(f"full-record decode requires width {WIDTH}, got {width}")
    records = (height + 1) // 2
    expected_size = records * FULL_RECORD_BYTES
    if path.stat().st_size != expected_size:
        raise ValueError(f"stream has {path.stat().st_size} bytes, expected {expected_size}")
    if chunk_records <= 0:
        raise ValueError("chunk_records must be positive")

    words = np.memmap(
        path,
        dtype=">u2",
        mode="r",
        shape=(records, FULL_RECORD_WORDS),
    )
    layout_report: dict[str, object] = {}
    if validate_padding:
        layout_report = validate_full_record_layout(words)

    rgbi = np.empty((height, width, CHANNELS), dtype=np.uint16)
    rgb_offsets_words = tuple(tuple(offset // 2 for offset in sample_offsets) for sample_offsets in FULL_RGB_SAMPLE_BYTE_OFFSETS)
    ir_offset_words = FULL_IR_BYTE_OFFSET // 2

    for first in range(0, records, chunk_records):
        last = min(records, first + chunk_records)
        block = words[first:last]
        count = last - first
        rgb_sum = np.zeros((count, RGB_CHANNELS, UNIT_WORDS), dtype=np.uint64)
        for sample_offsets in rgb_offsets_words:
            for channel, offset in enumerate(sample_offsets):
                rgb_sum[:, channel] += block[:, offset : offset + UNIT_WORDS]
        rgb_avg = ((rgb_sum + 2) // 4).astype(np.uint16)
        rgb_rows = rgb_avg.reshape(count, RGB_CHANNELS, width, ROWS_PER_RECORD).transpose(0, 3, 2, 1)
        ir_rows = block[:, ir_offset_words : ir_offset_words + UNIT_WORDS].reshape(count, width, ROWS_PER_RECORD).transpose(0, 2, 1)

        chunk = np.empty((count, ROWS_PER_RECORD, width, CHANNELS), dtype=np.uint16)
        chunk[..., :RGB_CHANNELS] = rgb_rows
        chunk[..., 3] = ir_rows
        flat_rows = chunk.reshape(-1, width, CHANNELS)
        output_first = first * ROWS_PER_RECORD
        output_last = min(height, output_first + flat_rows.shape[0])
        rgbi[output_first:output_last] = flat_rows[: output_last - output_first, ::-1, :]

    layout_report.update(
        {
            "rgb_samples_decoded": 4,
            "rgb_average": "round-half-up uint16 average",
            "ir_planes_transferred": 1,
            "ir_multisample_semantics": "firmware-combined-or-1x-unresolved",
        }
    )
    return rgbi, layout_report


def decode_records(
    path: Path,
    *,
    record_bytes: int,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> tuple[np.ndarray, dict[str, object]]:
    if record_bytes == FULL_RECORD_BYTES:
        return decode_full_records(path, width=width, height=height)
    return (
        decode_core_records(
            path,
            record_bytes=record_bytes,
            width=width,
            height=height,
            channels=CHANNELS,
        ),
        {
            "rgb_samples_decoded": 1,
            "ir_planes_transferred": 1,
            "prefix_capture": True,
        },
    )
