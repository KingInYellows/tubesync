---
title:
  'AI review bots re-review every push; stop after one wave, and use REST when
  GraphQL is rate-limited'
date: 2026-09-25
category: workflow
track: knowledge
problem:
  'each push to the stack drew a fresh wave of Codex/Cursor/CodeRabbit threads,
  so fix-push cycles never converged, and heavy thread handling hit GitHub''s
  GraphQL secondary rate limit mid-run'
tags: [code-review, bots, codex, cursor, github-api, rate-limit, graphite]
components: ['.github']
---

## Context

The fork's PRs are reviewed by several bots (chatgpt-codex-connector, Cursor
Bugbot/Automation, CodeRabbit, cubic, Sourcery). Each push re-triggers them,
and Codex in particular finds a new edge case on almost every pass. Over one
day the Plex stack went through four waves; each fix-push produced another.

Handling many threads (reading `reviewThreads`, resolving, editing PR bodies
with `gh pr edit`) also exhausted GitHub's GraphQL limits. `gh api rate_limit`
still reported most of the quota remaining, but calls failed with
`graphql_rate_limit`, which points to the secondary (burst) limit.

## Guidance

- Plan one fix pass per review wave, push once, then **triage** the next wave
  into three groups: real bug, judgment call, and duplicate or already fixed.
  Report that list to the user instead of fixing it automatically.
- Resolve Cursor's "Verified ..." threads **without replying**. Replies
  re-trigger Cursor automation and can loop.
- When GraphQL is limited, use REST, which has its own quota:
  - read a PR: `gh api repos/OWNER/REPO/pulls/N`;
  - edit a body: `gh api -X PATCH repos/OWNER/REPO/pulls/N -F body=@file`;
  - reply to a thread: `gh api -X POST repos/OWNER/REPO/pulls/N/comments/<id>/replies -F body=@file`;
  - check CI: `gh api repos/OWNER/REPO/commits/<sha>/check-runs`.
- Resolving threads needs GraphQL (`resolveReviewThread`). Space the calls by
  a few seconds and retry in the background until they succeed.
- Get an independent read-only review of your *own* fix commits before
  pushing. It catches most of what the next bot wave would raise, in one
  round instead of several.

## Why This Matters

Without a stopping rule, each push costs a full cycle, and the bots' marginal
findings get more speculative while the diff grows. The rate limit fails
silently in scripted loops: a CI poll died on it here.

## When to Apply

Any stacked-PR sweep on a repository with several AI reviewers enabled.

## Examples

Useful poll: count unresolved threads per PR with GraphQL `reviewThreads`
(filter `isResolved == false`). Fall back to REST `pulls/N/comments` when
GraphQL is refused.
