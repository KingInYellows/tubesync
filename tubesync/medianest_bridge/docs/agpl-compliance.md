# AGPL obligations (T5)

This document states verified facts about this fork's license posture and
flags open questions for legal review. It does not draw legal
conclusions ("this fully satisfies AGPLv3 §13") -- only a qualified
reviewer should make that call; what follows is the factual record such a
review would start from.

## Verified facts

1. **Upstream `LICENSE` is byte-identical, unmodified, across the entire
   fork chain.** Checked directly, not assumed:
   ```
   git diff main -- LICENSE                                              # empty
   git diff 3b9d72f28dda9c931776f76e7428a72a24a57f82 -- LICENSE           # empty
   ```
   (the second command diffs against the pinned upstream-base commit
   itself, per `medianest_bridge/docs/UPSTREAM_SHA`). The file is the
   standard GNU Affero General Public License v3, 19 November 2007 text,
   at the repository root, exactly where upstream keeps it.

2. **No pre-existing `NOTICE`/`COPYING`/similar file exists anywhere in
   this repository, upstream or fork.** Checked directly:
   ```
   find . -maxdepth 3 \( -iname "LICENSE*" -o -iname "NOTICE*" -o -iname "COPYING*" \)
   ```
   returns only the one root `LICENSE` file, in both this fork and (per
   the T1 upstream audit) upstream itself. There is no pre-existing
   convention this fork is breaking by not adding one; adding one is a
   fork-side choice (see item 4 below), not a correction of an omission.

3. **The public fork repository is the AGPLv3 §13 corresponding-source
   offer.** Verified live, not assumed:
   ```
   $ gh repo view KingInYellows/tubesync --json visibility,isPrivate,url
   {"isPrivate":false,"url":"https://github.com/KingInYellows/tubesync","visibility":"PUBLIC"}
   ```
   The repository is genuinely public at the time of this check. AGPLv3
   §13 requires that users interacting with a modified version over a
   network be offered the corresponding source of that exact modified
   version; a public GitHub repository containing that source, reachable
   at a stable URL, is the offer this fork makes. This document does not
   independently verify that every future deployment of an image built
   from this fork actually surfaces this URL to end users in a compliant
   way (e.g., a MediaNest-side "source" link or equivalent) -- that is a
   deployment-configuration fact outside this repository's own code, not
   something `medianest_bridge`'s source can enforce or verify from
   inside itself. **Flagged for legal review**: whether the *bridge
   API's own responses* need to carry any explicit source-offer
   signaling (a response header, a `/meta` field), or whether the public
   repository URL alone (documented in this fork's own README and now in
   this doc) is a sufficient offer as currently deployed. This repo takes
   no position on that question.

4. **No new dependencies were added by T1-T5.** Every prior slice's PR
   body states this explicitly ("No new pip dependencies"); T5 adds a
   `Dockerfile` `ARG`/`ENV` pair, a new fork-owned GitHub Actions workflow
   file, and documentation -- no new runtime or build dependency. This
   means T1-T5 introduce no new third-party license obligations beyond
   what upstream TubeSync's own `Pipfile` already carries (out of scope
   for this fork to audit -- upstream's own dependency licensing is
   upstream's responsibility, unchanged by this fork).

5. **No Copyright/SPDX markers were found in `medianest_bridge/`.**
   Checked directly (`grep -rln "Copyright\|SPDX-License" medianest_bridge
   --include="*.py"` returns nothing). That is limited evidence, not
   proof that no third-party code was copied -- files can omit those
   markers. The app is still built from TubeSync's own models, forms,
   and task-scheduling primitives as described in `README.md`.

## Open item, flagged rather than resolved here

**The vendored contract file
(`medianest_bridge/contract/bridge-openapi.v1.yaml`) carries no license
header of its own**, and its canonical source is a *different*
repository (`KingInYellows/medianest`) with potentially different
licensing than this AGPLv3 fork. Verified: the file, as vendored, has no
`# Copyright`/`# License`/SPDX line anywhere in it (checked directly). Two
facts, not a conclusion: (a) an OpenAPI contract document is arguably a
specification/interface description rather than executable code subject
to the same copyleft concerns as a code file, and (b) this repository has
no visibility into the MediaNest repository's own license (out of this
agent's scope per this program's repository-boundary rules -- see the
workspace `CLAUDE.md`). **Flagged for legal review**: confirm the
MediaNest repository's license permits vendoring this file's text into an
AGPLv3-licensed repository, and whether a license/attribution header
should be added to the vendored copy.

## Fork-delta NOTICE statement

The following statement is added to `medianest_bridge/README.md`'s
existing "License" section (T5), formalizing what that section already
implied into an explicit fork-delta notice:

> This app (`medianest_bridge`) is original code added by the
> `KingInYellows/tubesync` fork; it is not present in upstream
> `meeb/tubesync` at the pinned upstream-base commit
> (`medianest_bridge/docs/UPSTREAM_SHA`). It is licensed identically to
> the rest of this repository, AGPLv3, under the unmodified `LICENSE` at
> the repository root. The five upstream files this fork's delta touches
> (`medianest_bridge/README.md`'s "Fork delta" section lists them exactly)
> remain licensed as upstream TubeSync itself is licensed, modified only
> as that section describes.

See `medianest_bridge/README.md`'s "License" section for the statement as
actually placed in that file.
