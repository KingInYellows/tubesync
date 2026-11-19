# ADR-0002: Artist-first episode titles and show title override (MED-702)

Status: accepted (2026-11-19)

## Context

The Plex "YouTube" TV library (section 6) reads TubeSync's output with
the Plex NFO Series agent. Seasons are upload years (`Season 2026/`, hidden
in Plex with flattenSeasons) and episodes are `sYYYYeMMDDNN`. MEDIA-OPS
asked for the "Live Music" playlist's episode titles to start with the
artist, in both the file name and the NFO `<title>`, applied to files
already on disk without downloading again.

The show folders (`acq-src-<key>`) are the stable key for Kometa labels,
the Kometa asset folder and Posterizarr manual assets, so they do not
change. Neither do the download location, mounts or `/data` paths.

## Considered and dropped

- **Season posters** (`Season YYYY/SeasonYYYY.jpg`, the name in
  https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/).
  Dropped by the owner: seasons are hidden in the library, and the only
  source image would be the show folder's `season-poster.jpg`, which is the
  336x188 playlist thumbnail for most sources. Season art, if wanted, goes
  through Kometa/Posterizarr assets.
- **Renaming the show to "Live Sessions"**: kept as "Live Music" so it
  matches the custom poster. The command below can still set a title
  (`--show-title`) if MEDIA-OPS makes a matching poster first.

## Decision

1. **Artist-first titles**: two new `media_format` keys, `{artist_title}`
   and `{artist_title_bounded}`. A source opts in by using one; its episode
   NFO `<title>` then uses the same text, so the file name and Plex agree.
   The artist comes from the video's YouTube music credit (`artists` /
   `artist` metadata). Without one, a title already written
   "Artist | Title" or "Artist – Title" gets " - " as its separator, and
   any other title is left alone: the uploading channel is too often a
   venue, label or TV show to stand in for the artist. No other source
   changes.
2. **Show title override (available, not used now)**: no new model field
   (the fork carries no migrations of its own). `sync/tvshow_nfo.py`
   already keeps a `tvshow.nfo` it did not write, and every episode
   `<showtitle>` (and the bridge's source `title`) follows that file's
   `<title>`. `--show-title` writes such a hand-owned file with its
   `<uniqueid>`s kept. Deleting `tvshow.nfo` returns to the automatic
   title.
3. **Existing files**: `manage.py medianest_plex_naming` (dry-run unless
   `--apply`). It saves the new `media_format` with a queryset update (no
   post_save cascade), then renames and rewrites the NFO of each downloaded
   media in its own short transaction, under the same huey locks as
   `rename_all_media_for_source` (MED-695: no lock held across the run).
   Busy media are skipped and make the command exit non-zero; run it again.
   A re-run also finishes an interrupted rename: it records a video that
   was moved but not saved, and moves sidecars still named after the old
   file (never overwriting; partial downloads are left alone). Both need
   the file stem to end with `[{key}]`, as the bridge formats do.

## Runbook (dockerdownload, after the image with this change is deployed)

```sh
# 1. preview: prints the old -> new paths
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming \
  --source PLyEEB-uDvBD9ijQ3wjLkHjlsCEQCvG-ET --artist-titles --examples 50

# 2. apply
docker exec -u app tubesync python3 /app/manage.py medianest_plex_naming \
  --source PLyEEB-uDvBD9ijQ3wjLkHjlsCEQCvG-ET --artist-titles --apply

# 3. Plex: scan the YouTube library; the show should still list every episode
```

## Consequences

- Live Music episode files are renamed in place (same folders, same
  `[videoid]`, same `youtube_<id>` episode GUIDs). With autoEmptyTrash on,
  Plex shows a rename as a delete plus re-add; watch state on those
  episodes can reset.
- Kometa and Posterizarr keep matching: show and season folder names do
  not change.
- `medianest_backfill_plex_sidecars --all-bridge-sources` applies the
  bridge profile's `media_format`, which would switch Live Music back to
  plain titles. Leave that source out of such a run (or re-run
  `medianest_plex_naming --artist-titles` after it).
