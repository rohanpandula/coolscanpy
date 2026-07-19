# Changelog

## 0.1.3

A live hardware session scanning a 6-frame strip found two bugs in the
fine-scan path, both specific to strips shorter than a full roll.

The frame table sent to SEND(0x8f) required at least 37 scanner-addressable
records, a full-roll assumption left over from when every roll filled all
37 slots. The live mapping's record count now only has to cover every
requested slot in the batch, with a floor of 2. When a reviewed roll
fingerprint is available at the same point, the live table's addressable
count is also checked against the fingerprint's frame count, tolerating a
difference of one for a trailing sliver that crosses the 16-row
visual-signing threshold between traversals.

A preview traversal of a short strip parks the transport at its physical
end-stop. The next fine-scan attempt's fresh index read then fails with a
non-zero status on command 64. That failure now raises a new
`RefeedRequired` instead of a generic protocol error, with a message
telling the operator to pull the strip out, reinsert it until the feeder
grips, and retry the batch. No automatic eject or retry is attempted.

## 0.1.2

The exclusive output lock in the full-negative capture workflow no longer
hard-imports `fcntl`, which does not exist on Windows and broke test
collection there. Windows now uses `msvcrt` byte-range locking, and the
lock is only released when it was actually acquired.

CI runs on Linux and macOS. Windows stays out of scope for now: the
transport layer has never run there and the concurrency tests assume
POSIX lock semantics. The README says the same.

## 0.1.1

Fixes a bug found during the first live-hardware validation of 0.1.0: a
roll whose strip end leaves a trailing sliver shorter than 16 preview rows
crashed `build_reviewed_roll_fingerprint` on both the reviewed and the
fresh traversal. Sliver intervals are now skipped deterministically on
every traversal, frame native origins are filtered in lockstep with the
visual hashes, and an all-sliver roll raises a clear `ValueError`.

Live validation results from 2026-07-18, using the packaged wheel in a
clean environment against an LS-5000: SANE-free USB enumeration, `open()`,
option introspection, and a full roll preview of a 6-slot strip. Slots 1
through 4 aligned automatically at offset 0, slot 5 was flagged for manual
review near the strip end, and slot 6 was correctly reported as a 2-row
trailing sliver rather than a frame.

## 0.1.0

Initial standalone release, extracted from a NegPy integration branch.

Public API:

- `coolscanpy.get_devices()` / `coolscanpy.open()`: python-sane-shaped device
  enumeration and open, scoped to Nikon Coolscan LS-5000 units.
- `Device`: typed option attributes (`resolution`, `depth`, `samples`,
  `autofocus`, `auto_exposure`) with constraint validation, option
  introspection through `Device.option_names` and `Device[name]`,
  `scan()`, `cancel()`, `eject()`.
- `Device.roll()`: the 40-slot roll-feeder extension. `Roll.preview()`,
  `Roll.spacing_offset()` / `set_spacing_offset()`, `Roll.approve()` /
  `needs_approval()`, `Roll.fingerprint`, `Roll.scan()` /
  `scan_many()`, `Roll.safe_stop()`, `Roll.eject()`.
- `Frame` and `Receipt`: in-memory RGB, aligned infrared plane, and
  per-frame provenance (exposure, clipping, focus detail, transport-smear
  assessment, fingerprint hashes, artifact hashes).
- A typed exception hierarchy rooted at `PyCoolscanError`, including
  `DeviceBusy`, `FingerprintRefused`, `ManualReviewRequired`,
  `SafeStopRequested`, `FeederParked`, `TransportSmearDetected`, and
  `BatchIntegrityError`.

Streaming: `Roll.scan_many()` yields each frame as it completes instead of
buffering the whole batch, keeping memory bounded to roughly two frames in
flight regardless of how many slots are requested.

Implemented end to end for `Material.COLOR_NEGATIVE` (the direct-USB
single-pass RGBI4 route). `Material.BLACK_AND_WHITE_NEGATIVE` previews and
approves correctly; its fine-scan path is not yet wired to the roll batch
engine and raises `NotImplementedError`.

Packaging: `py.typed` marker, publish-grade `pyproject.toml` metadata,
optional `[scanner]` extra for the SANE-backed plain-scan path, console
scripts `coolscanpy-practical-parity` and `coolscanpy-roll-scan`.

Testing: 807 hardware-free tests across the transport protocol, roll
engine, receipt assembly, and public facade, run against synthetic
fixtures and replay data. No test in this suite touches real hardware.

CI: GitHub Actions matrix across Ubuntu, macOS, and Windows on Python 3.13
and 3.14, running pytest and ruff.
