# Solution notes

Fork-maintainer notes on problems solved while building the MediaNest bridge and the Plex TV library work. Each note records the problem, what didn't work, the fix and how to avoid it next time. These are fork-only files; upstream `meeb/tubesync` has no `docs/solutions/`.

- `code-quality/date-episode-numbers-collide-with-downloaded-files.md`: why a downloaded file keeps its episode number.
- `code-quality/rename-files-key-sweep-moves-other-media.md`: the second, `{key}`-based pass of `Media.rename_files()`.
- `code-quality/yt-dlp-channel-title-has-tab-suffix.md`: use `channel`, not the tab-suffixed page `title`.
- `workflow/django-tests-in-the-release-image.md`: running the suite and CI's lint locally.
- `workflow/graphite-restacks-miscount-fork-delta-docs.md`: re-checking the fork-delta counts after a restack.
- `workflow/bot-review-waves-and-github-rate-limits.md`: stopping review loops, and REST fallbacks for GitHub rate limits.
