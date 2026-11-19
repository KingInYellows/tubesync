# ADR-0002: Plex season posters, artist-first titles and show title override (MED-702)

Status: proposed (2026-11-19)

## Context

The Plex "YouTube" TV library reads TubeSync's output with the Plex NFO
Series agent. Seasons are upload years (`Season 2026/`) and episodes are
`sYYYYeMMDDNN`. MEDIA-OPS asked for three changes:

1. Season artwork. TubeSync writes `season-poster.jpg` in the show
   folder, which Plex does not read.
2. The "Live Music" playlist: episode titles that start with the artist,
   in both the file name and the NFO `<title>`, and the show shown as
   "Live Sessions".
3. Both applied to files already on disk, without downloading again.

Show posters stay with Kometa. The download location, the show folder
names (`acq-src-<key>`) and the hardlink-friendly `/data` paths do not
change.

## Plex docs checked

- Local Media Assets, TV Shows
  (https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/):
  a season poster is `ShowName/Season XX/SeasonXX.ext`, inside the season
  folder. `poster.jpg` inside a season folder is not a documented name.
- Using NFO metadata files
  (https://support.plex.tv/articles/using-nfo-metadata-files-with-plex/):
  the NFO agent prefers local image assets to remote ones, and points to
  the page above for their names.

## Decision

1. **Season posters**: each `Season YYYY/` folder gets `SeasonYYYY.jpg`, a
   copy of the show folder's `season-poster.jpg` (or `poster.jpg` when that
   is missing). A season poster is only created, never replaced, so a file
   placed by hand stays. It is written after each download (a new year's
   first video), after `download_source_images`, after
   `rename_all_media_for_source`, and by the command below.
   `season-poster.jpg` itself is still written as before.
2. **Artist-first titles**: two new `media_format` keys, `{artist_title}`
   and `{artist_title_bounded}`. A source opts in by using one; its episode
   NFO `<title>` then uses the same text, so the file name and Plex agree.
   The artist comes from the video's YouTube music credit (`artists` /
   `artist` metadata). Without one, a title already written
   "Artist | Title" or "Artist – Title" gets " - " as its separator, and
   any other title is left alone: the uploading channel is too often a
   venue, label or TV show to stand in for the artist. No other source
   changes.
3. **Show title**: no new model field (the fork carries no migrations of
   its own). `sync/tvshow_nfo.py` already keeps a `tvshow.nfo` it did not
   write, and every episode `<showtitle>` (and the bridge's source
   `title`, which MediaNest shows) follows that file's `<title>`. The
   command writes such a hand-owned file: the current one with the new
   `<title>`/`<sorttitle>` and no TubeSync checksum. Its `<uniqueid>`s are
   kept, so Plex sees the same show. Deleting `tvshow.nfo` returns to the
   automatic title.
4. **Existing files**: `manage.py medianest_plex_naming` (dry-run unless
   `--apply`). It saves the new `media_format` with a queryset update (no
   post_save cascade), then renames and rewrites the NFO of each downloaded
   media in its own short transaction, under the same huey locks as
   `rename_all_media_for_source` (MED-695: no lock held across the run).
   Busy media are skipped and make the command exit non-zero; run it again.

## Runbook (dockerdownload, after the image with this change is deployed)

```sh
# 1. preview: prints the old -> new paths
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming \
  --source PLyEEB-uDvBD9ijQ3wjLkHjlsCEQCvG-ET \
  --artist-titles --show-title 'Live Sessions' --examples 50
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming --season-posters

# 2. apply
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming \
  --source PLyEEB-uDvBD9ijQ3wjLkHjlsCEQCvG-ET \
  --artist-titles --show-title 'Live Sessions' --apply
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming --season-posters --apply

# 3. Plex: scan the YouTube library, then refresh the Live Sessions show
```

## Consequences

- Live Music episode files are renamed in place (same folders, same
  `[videoid]`). Plex may treat a renamed file as a new item, so watch state
  on those episodes can reset, and MediaNest's stored Plex rating keys for
  them can go stale until its reconciler re-matches by video id.
- Kometa collections that match the show folder keep matching: the show
  and season folder names do not change.
- New `Season YYYY/` folders get a poster automatically from then on.
- `medianest_backfill_plex_sidecars --all-bridge-sources` applies the
  bridge profile's `media_format`, which would switch Live Sessions back
  to plain titles. Leave that source out of such a run (or re-run
  `medianest_plex_naming --artist-titles` after it).
