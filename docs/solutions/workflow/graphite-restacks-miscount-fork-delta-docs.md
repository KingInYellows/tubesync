---
title:
  'Restacking the Plex stack silently merges fork-delta counts to wrong
  totals'
date: 2026-09-25
category: workflow
track: knowledge
problem:
  'when a lower branch and an upper branch both change the fork-delta counts,
  git auto-merges identical line edits cleanly and keeps the lower branch''s
  totals, so the upper branch''s docs undercount without any conflict'
tags: [graphite, restack, fork-delta, docs, merge-conflicts]
components:
  [
    'tubesync/medianest_bridge/README.md',
    'tubesync/medianest_bridge/docs/upstream-sync.md',
    'tubesync/medianest_bridge/docs/agpl-compliance.md',
    'tubesync/medianest_bridge/docs/compatibility-matrix.md',
  ]
---

## Context

Four docs state how many upstream files and touch points the fork changes:

- the bridge `README.md` "Fork delta" section;
- `docs/upstream-sync.md`;
- `docs/agpl-compliance.md`;
- `docs/compatibility-matrix.md`.

In a stacked set of PRs where more than one branch adds an upstream touch
point, each branch carries its own totals. Adding a touch point at a lower
branch and restacking produces two kinds of trouble:

- **Silent miscounts.** If both branches changed a line in the same way (for
  example "seven" to "eight"), git merges it cleanly. The upper branch keeps
  the lower branch's total instead of its own, larger one.
- **Real conflicts.** The numbered list and the file lists conflict in the
  README and the compatibility matrix.

## Guidance

- After every restack, check all four docs at every tip, not just the ones
  that conflicted:

  ```bash
  git show "${branch}:tubesync/medianest_bridge/README.md" | grep -n 'upstream files are touched'
  ```

  Do the same for the other three files. Use `"${branch}:path"`: in zsh,
  `$branch:t` is a history modifier.
- Keep a small script that rewrites the totals by pattern (files, points,
  minimal), and run it after resolving each conflict.
- Where a file conflicts on every replayed commit of an upper branch (here
  `sync/tvshow_nfo.py` in the backfill branch), build the intended final file
  once. Copy it in at each conflict, then confirm with `cmp` that the tip
  equals it; later commits can otherwise auto-merge into something else.
- Continue with `gt continue`, never `git rebase --continue`. Keep Bash
  commands that mix `git`, `gh` and `gt` short; a hook blocks long compound
  ones.
- Before `gt submit`, compare heads and bases on GitHub. Run
  `git range-diff origin/<base>..origin/<branch> <base>..<branch>` and expect
  only your new commits plus the known conflict resolutions.

## Why This Matters

The fork's AGPL notice and upstream-sync process both rely on these counts.
A wrong total never fails a test, so the only guard is to check.

## When to Apply

Any edit to a lower branch of a stack in which more than one branch adds an
upstream touch point.

## Examples

At the time of writing, the Plex TV stack's totals are: eight upstream files,
nine points, six minimal at `plex/t1-episode-numbering`; nine files, ten
points, seven minimal from `agent/plex/t2-tvshow-nfo` up.
