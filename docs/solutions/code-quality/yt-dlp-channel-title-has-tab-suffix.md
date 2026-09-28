---
title: 'yt-dlp''s channel-page "title" carries the tab name; use "channel"'
date: 2026-09-25
category: code-quality
track: bug
problem:
  'tvshow.nfo took the show title from the cached channel metadata''s "title",
  which yt-dlp''s YoutubeTab extractor sets to "<name> - Videos" (or another
  tab), not the channel name'
tags: [yt-dlp, youtube, tvshow-nfo, plex, metadata]
components: ['tubesync/sync/tvshow_nfo.py', 'tubesync/sync/youtube.py']
---

## Problem

`sync/youtube.py:get_image_info()` caches the raw channel or playlist page
response as a `Metadata` row (`source` and `media` both NULL, `site` =
`YoutubeTab`). `sync/tvshow_nfo.py` used that row's `title` as the show
title. For a channel, yt-dlp's `YoutubeTabIE._extract_from_tabs()` appends the
selected tab:

```python
metadata['title'] += format_field(selected_tab, 'title', ' - %s')
```

So the show came out as "Some Channel - Videos". The clean name is in
`channel` (from `channelMetadataRenderer.title`). For a *playlist*, `title` is
the playlist title and is correct.

## Symptoms

- A Plex show named "Channel Name - Videos" or "Channel Name - Home".
- Episode `<showtitle>` (from per-video metadata `channel`) disagrees with the
  show's `<title>`.

## What Didn't Work

- **Stripping a trailing " - <Tab>" with a regex.** Tab names are localized
  and can change. A real channel name can also end in " - Something".

## Solution

Read `channel`, then `uploader`, then `title` from the cached row for channel
sources, and `title` for playlists (`_resolve_show_title_from_data()` in
`sync/tvshow_nfo.py`, commit `741a8424`). Then, when per-video metadata was
retrieved *after* the cached row, prefer the video's `channel`: a renamed
channel's new videos carry the new name before `download_source_images`
refreshes the cache.

## Why This Works

`channel` comes from the same renderer as the page title but without the tab
decoration. Because both tiers now read the same field, comparing them by
retrieval time is meaningful and cannot flip between two formats.

## Prevention

- Check the extractor source in the image before trusting a yt-dlp field's
  shape. The image ships yt-dlp; read
  `yt_dlp/extractor/youtube/_tab.py` there.
- Test: `test_cached_channel_row_prefers_channel_over_its_tab_title` in
  `sync/tests/test_tvshow_nfo.py`.
