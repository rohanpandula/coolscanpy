# Live Validation Addendum — multi-batch-per-feed

Branch: `port/multi-batch-per-feed` (from `port/cross-platform` HEAD, commit
6616c3b). Offline-only: no scanner was touched to produce this branch —
everything below is verified against the hardware-free test suite only.
This document is the owner's checklist for the first live session against
real hardware.

## What this branch changes

Closes GitHub issue #30: a second `scan_many()` call mid-roll previously
opened a **fresh** transport reservation instead of resuming the one
`preview()` (or the prior batch) already held — replaying the seq-88
SET_WINDOW preamble roughly 30 frames into the roll, which the scanner
answers with sense `052600`. This branch ports `bc4e7ac`
("multi-batch-per-feed — the hold survives batch completion; one reservation
from feed to eject, vendor-exact") from
`coolscanpy-roll-capture`/`fix/last-slot-origin-clamp`, plus the two
prerequisite commits its own hold-reservation semantics build on
(`e5b0ac4` — the held-preview foundation, `04381cf` — traced software
eject) that turned out to be entirely absent from `port/cross-platform` —
see the port report (returned alongside this addendum) for why those had
to come along and could not be skipped.

After this branch: `preview()` keeps its reservation open; every
`scan_many()`/`scan()` call that follows — not just the first — resumes that
same reservation instead of opening a new one, indefinitely, until
`eject_after=True`, `Roll.eject()`, `Roll.release()`, or `Roll.close()` ends
it. This is the vendor's own traced session shape: one `RESERVE_UNIT` from
feed to eject, any number of fine scans in between.

## What to watch in the live session

Run a full multi-batch roll: 36 frames, with the second `scan_many()` batch
triggered mid-roll (e.g. call `scan_many()` twice — slots 1–20 then
21–36 — without an intervening `preview()`, `release()`, or `eject()`).

1. **Frame 31+ scans clean.** No `sense 052600` on any frame past the
   ~30-frame mark — this is the exact failure signature issue #30 reports.
   Every frame across both batches should complete normally.
2. **No fresh `RESERVE_UNIT` between batches.** Pull the capture journal
   for the second batch's first frame and confirm it does NOT show a new
   reservation sequence (seq-17 `RESERVE_UNIT` should appear exactly once
   for the whole roll, not once per batch). The per-frame journal's
   `session_reservation_retained: true` / `unit_released: false` fields
   (present on every frame except the roll's true last one) are the
   software-side corroborating evidence.
3. **One reservation, feed to eject.** The session journal
   (`session-journal.json` in the attempts directory) should show a single
   `status: "held"` → `"capturing"` cycle repeating across both batches'
   frames, then exactly one release (`unit_release_attempts: 1`,
   `unit_released: true`) at the very end — after an explicit `eject()` or
   `scan_many(..., eject_after=True)` on the final batch, not one per
   batch. If `eject_after=True` was used, `session_journal["eject"]`
   should be present with `terminal_sense` matching the traced
   end-of-session value.
4. **No repeated command-64.** The `VARIABLE_FRAME_TABLE_SEQUENCE`
   (command 64, the frame-table transaction) should fire once, during the
   initial `preview()`, never again during either batch.

If any of 1–4 fails, the branch has not reproduced the vendor-exact
behavior the offline suite validated and should not be considered ready —
see the rollback story below; no mainline state needs to change either way.

## Offline verification already done

Full suite (`.venv/bin/python -m pytest tests/`) green on this branch,
including every ported and hand-adapted test — see the port report for
exact counts and file-by-file breakdown. Nothing below this line was
exercised against real hardware.

## Rollback story

This work lives entirely on `port/multi-batch-per-feed`, a branch created
from `port/cross-platform` HEAD and never merged back into it or into
`main`, and never pushed anywhere. `port/cross-platform` and `main` are
byte-for-byte untouched by this port. If live validation fails or is
inconclusive:

- No rollback action is needed on `port/cross-platform`/`main` — they were
  never touched.
- Abandon or reset `port/multi-batch-per-feed` locally; nothing external
  depends on it.
- The prior behavior (each `scan_many()` opens its own fresh reservation,
  issue #30's failure mode included) is exactly what `port/cross-platform`
  still does today, unchanged.

## If live validation passes — reporter-facing follow-ups for issue #30

- Close #30 citing this branch's commit range and the specific evidence
  from the live session (frame index where the second batch resumed,
  confirmation of a single reservation-open/reservation-close pair for the
  whole roll, absence of sense `052600` past frame 30).
- Note the behavior change explicitly in the closing comment:
  `scan_many()` without `eject_after=True` now holds the reservation by
  default instead of releasing after every batch — any caller that relied
  on the old "every batch releases" behavior (e.g. to force a refeed
  between batches) must now call `roll.release()` explicitly.
- Flag `Roll.eject()`'s behavior change for anyone tracking the public API:
  it now replays the vendor-traced software eject sequence through the
  held reservation instead of delegating to the plain SANE-based
  `Device.eject()` — a suspected transport wedge during that sequence
  raises `FeederParked` (power-cycle required) rather than falling back
  silently.
- Only after live validation passes: port this same change into the
  ScanStudio vendored copy and the ports mirror — explicitly out of scope
  for this branch (see the port report's own note on this).
