---
title:
  'Media.rename_files() has a second {key} rglob pass that a preflight must
  model'
date: 2026-09-25
category: code-quality
track: bug
problem:
  'the backfill''s dry-run and safety preflight modelled only rename_files()''s
  old-stem sidecar moves, but with {key} in media_format it also moves every
  path under the source directory whose name contains the media key'
tags: [rename-files, backfill, dry-run, filesystem-safety, upstream-behavior]
components:
  [
    'tubesync/sync/models/media.py',
    'tubesync/sync/management/commands/medianest_backfill_plex_sidecars.py',
  ]
---

## Problem

Upstream `Media.rename_files()` moves more than the video and its same-stem
sidecars. After moving the video it collects two sets:

1. **Stem pass:** every file next to the old video whose name starts with the
   old stem. These are moved with `Path.replace()`, which **overwrites** an
   existing destination.
2. **Key sweep:** when `'{key}' in media_format`, it runs
   `source_dir.rglob('*' + key + '*')`. Every match (files *and directories*,
   anywhere under the source directory) is renamed next to the new video under
   the new stem plus its own suffixes. A match whose destination already
   exists is silently skipped and left behind.

`medianest_backfill_plex_sidecars` promised that a dry-run lists every move
`--apply` makes, and its preflight refused unsafe renames. It modelled only
set 1, so `--apply` could move files the dry-run never mentioned. That
included another media's sidecar whose name happened to contain this key (its
row then points at a file that moved), or a whole directory.

## Symptoms

- After `--apply`, files appear in `Season YYYY/` that the dry-run did not
  list.
- Another media's subtitle or NFO ends up renamed to this media's stem.
- A directory whose name contains a media key is moved into a season folder.

## What Didn't Work

- **Checking only `glob(old_stem + '*')` beside the video.** That misses the
  recursive key sweep entirely.
- **Checking the key-sweep candidates against the set of media video paths
  only.** In a dry-run, other media processed earlier in the same run are
  "moved" only in the projected set, so their sidecars still sit under their
  *old* stems on disk. Check against both the original and the projected
  video paths.
- **Rejecting directories only in the key sweep.** The stem pass's plain
  `glob` returns directories too, and `Path.replace()` would move a directory
  tree. Filter both sets.

## Solution

`_key_matched_moves()` mirrors the key sweep exactly: the same rglob, the
same skips (the video itself, stem-pass files, paths already at their
destination), and a `taken` set for destinations the stem pass creates. It
returns (moves, collisions). The dry-run prints each move and counts
`key_matched_moves`. A media is refused, before anything moves, when any of
these holds:

- a match is another media's video, or a sidecar named `<its stem>.<suffixes>`
  (checked against where that video is now *and* where it was before the
  run);
- a match in either set is a directory;
- a match's destination is already taken (`rename_files()` would leave it
  behind);
- the current file is a symlink or resolves outside `DOWNLOAD_ROOT`, or the
  target directory resolves outside it.

## Why This Works

The preflight now runs the same selection logic as the code it guards, so
the dry-run and `--apply` agree on every path. Refusing, rather than trying
to reorder moves, keeps the command's contract: nothing moves unless every
move is known to be safe.

## Prevention

- Before relying on an upstream mutator in a "safe" wrapper, read the whole
  function. `rename_files()` has two loops with different overwrite
  semantics (`replace()` overwrites; the key sweep skips existing
  destinations).
- `Path.glob()`/`rglob()` return directories as well as files.
- Tests: `BackfillReviewFollowUpTestCase` and
  `BackfillReviewFollowUp3TestCase` in
  `sync/tests/test_backfill_plex_sidecars.py`.
