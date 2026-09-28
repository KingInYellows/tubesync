# Solution notes

Fork-maintainer notes on problems solved while building the MediaNest bridge and the Plex TV library work. Each note records the problem, what didn't work, the fix and how to avoid it next time. These are fork-only files; upstream `meeb/tubesync` has no `docs/solutions/`.

The three code-quality notes describe the Plex TV library stack, fork PRs [#16](https://github.com/KingInYellows/tubesync/pull/16), [#17](https://github.com/KingInYellows/tubesync/pull/17), [#18](https://github.com/KingInYellows/tubesync/pull/18) and [#19](https://github.com/KingInYellows/tubesync/pull/19). Until that stack merges, the code, tests and commits they cite exist only on those branches, not on `main`.

- [Why a downloaded file keeps its episode number](code-quality/date-episode-numbers-collide-with-downloaded-files.md) (#16).
- [The second, `{key}`-based pass of `Media.rename_files()`](code-quality/rename-files-key-sweep-moves-other-media.md) (#19).
- [Use `channel`, not the tab-suffixed page `title`](code-quality/yt-dlp-channel-title-has-tab-suffix.md) (#17).
- [Run the suite and CI's lint locally](workflow/django-tests-in-the-release-image.md).
- [Re-check the fork-delta counts after a restack](workflow/graphite-restacks-miscount-fork-delta-docs.md).
- [Stop review loops, and handle GitHub rate limits](workflow/bot-review-waves-and-github-rate-limits.md).
