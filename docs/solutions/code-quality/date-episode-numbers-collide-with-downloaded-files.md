---
title:
  'A live same-day episode index gives a later-indexed video an existing file''s
  name'
date: 2026-09-25
category: code-quality
track: bug
problem:
  '{episode_mmddnn} numbered same-day videos by their live position, so an
  earlier video indexed after a later one was downloaded took that file''s
  number, and yt-dlp (overwrites: None) attached the existing file to the new
  row'
tags: [episode-numbering, plex, yt-dlp, media-format, filenames]
components:
  ['tubesync/sync/models/media.py', 'tubesync/sync/tests/test_episode_numbering.py']
---

## Problem

`Media.episode_mmddnn` renders `MMDD` plus a two-digit same-day index for
Plex TV filenames (`s2026e091401`). The index was the item's *live* position
among the source's same-day media, ordered by (`episode_date`, `created`,
`key`). When an earlier-published video on an already-numbered day was indexed
later (the listing window grew, a premiere went public, or metadata moved an
item's approximate date onto that day), every later same-day item's live
number shifted up by one.

Files already on disk kept their old names. So the new item's live number was
the number an already-downloaded file still carried, and its download target
was that file's path. yt-dlp runs with `overwrites: None`, which skips an
existing video file ("has already been downloaded"). TubeSync then saw the
path exist and marked the new row downloaded against the *other* video's
file. A later rename of either row moved the shared file away from the other.

## Symptoms

- Two media rows whose `media_file` is the same path.
- A download task that finishes in seconds with "has already been
  downloaded" for a video that was never fetched.
- Plex shows one episode where two were expected, or an episode whose
  content does not match its NFO.

## What Didn't Work

- **Ordering the same-day index by discovery time (`created`) instead of
  publish time.** New uploads append, but an item whose approximate date
  moves onto an existing day when its metadata arrives still shifts the rest.
  It also misorders episodes within a day.
- **Relying on `rename_all_media_for_source` to renumber first.** It runs only
  after a source save, never between indexing and download, so the collision
  happens before any rename.
- **Storing the number in a new column.** That is an upstream model change
  and migration, which is a large fork delta for a numbering detail.

## Solution

Treat the downloaded file's name as the stored state (commit `2eb15045`,
refined in `256a5436`):

- `_episode_day_index_from_name()` reads the index back from `media_file.name`.
  It anchors on the literal text around `{episode_mmddnn}` in `media_format`
  (for the built-in profile, `e` before and ` - ` after), so a later title,
  format or extension change keeps the number. The token must encode the
  item's current `MMDD`. A field with no literal on either side is not read
  back.
- `Media._episode_day_index()`: a downloaded file keeps its index, and every
  other same-day item takes the *free* indexes, in same-day order, around the
  kept ones. One query over the day's rows gives both the live rank and the
  kept indexes.
- Only sources whose `media_format` uses `{episode_mmddnn}` do this; other
  sources keep the plain live index.

Result: a later-indexed earlier video gets the next free number, never a file
that already exists, and the numbering is independent of processing order.

## Why This Works

Assigning free numbers by rank is stable under freezing. When one of the
non-frozen items downloads at its assigned number, removing that number from
the free list and that item from the rank list leaves every other item's
assignment unchanged. So download order never renumbers anything, and a
number, once on disk, is never handed to another item.

## Prevention

- Any scheme that derives a filename from a *live* position must answer
  "what happens to files already written under the old position?"
- yt-dlp's default `overwrites` means a colliding target is silently treated
  as done. Treat any filename collision as a data-integrity bug, not a
  cosmetic one.
- Tests: `FrozenEpisodeNumberTestCase` in
  `sync/tests/test_episode_numbering.py`. Mutation-check it by making
  `_episode_day_index` return `_same_day_index()`; six tests fail.
