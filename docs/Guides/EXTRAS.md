# Extras: where they end up

When you label a title as anything other than an episode or the main feature —
Behind The Scenes, Featurette, Trailer, Deleted Scene and so on — MKV-Auto files
it in the folder your media server scans for extras.

## Series extras: the Belongs to control

On a series disc, every extra has a **Belongs to** control — a season dropdown
and an episode dropdown.

- **Whole series** (season blank):
  `Series/Star Wars Rebels/Behind The Scenes/Rebels Recon.mkv`
- **A season**:
  `Series/Star Wars Rebels/Season 03/Behind The Scenes/Rebels Recon.mkv`
- **A specific episode** (season + episode): on Plex the extra attaches to the
  episode itself, by filename rather than folder:
  `Season 07/Game of Thrones - s07e03 - The Queen's Justice-Winterfell-deleted.mkv`
  The middle segment is the extra's own Title name, so several named extras can
  attach to one episode. On Jellyfin — which has no episode-level extras — the
  file goes in the season's extras folder instead, and the episode choice
  affects only the data (see below).

When everything on a disc belongs to one season, extras pick that season up
automatically — the control shows it with an *auto* tag. Clear it for anything
that covers the series as a whole. Season `0` is the specials folder, the same
as it is for episodes.

Whatever you choose is **recorded in full for TheDiscDB**, whichever media
server you run: a Jellyfin user tagging a deleted scene to its episode is
contributing exactly the same data a Plex user would, even though their own
library only files it by season.

The episode attachment needs the episode to be on the same disc (its title
forms the filename prefix). An extra referencing an episode from another disc
falls back to the season folder.

One Plex quirk, verified on a live server: a plain library scan indexes the
file but does not attach extras — they appear after the show's metadata
refreshes. If a new extra doesn't show up, use **Refresh Metadata** on the
show, or wait for Plex's scheduled maintenance to do it.

## Folder names by server

The folder is chosen from the title's type and your **Media server** setting
(Settings → Library). Plex uses title case, Jellyfin lowercase.

| Type | Plex | Jellyfin |
| --- | --- | --- |
| Behind The Scenes | `Behind The Scenes` | `behind the scenes` |
| Deleted Scene | `Deleted Scenes` | `deleted scenes` |
| Featurette | `Featurettes` | `featurettes` |
| Interview | `Interviews` | `interviews` |
| Scene | `Scenes` | `scenes` |
| Short | `Shorts` | `shorts` |
| Trailer | `Trailers` | `trailers` |
| Other | `Other` | `other` |

Jellyfin has folders Plex does not — extras, samples, clips, theme-music,
backdrops. Those types are written to Plex's `Other` folder instead, which is
where Plex expects anything outside its fixed set.

Set the media server **before** you transfer. Changing it later renames nothing
that has already been written.

## Plex: agent notes

On the current TV agent (`tv.plex.agents.series`) no library setting is needed —
episode-attached extras were verified working on a live server with all
settings at their defaults. The older guidance about enabling **"Assign Extras
to Episodes, Seasons or Shows based on folder structure"** applies only to the
legacy TheTVDB/TheMovieDB agents, where that option still exists.

Episode-attached extras are **filename-only** on the current agent. The
subdirectory layout some guides describe (a folder named after the episode,
with extras inside) does not work: tested live, files placed that way are not
ingested at all — not as extras at any level, and not as episodes. Only the
`<episode filename>-<Name>-<type>` form attaches, which is what MKV-Auto
writes.

Be aware that Plex client support for season- and episode-level extras is
uneven across apps. If an extra does not appear on one client, check another
before assuming the file is wrong.

Jellyfin needs no equivalent setting.

## After adding extras

Both servers need to rescan before new extras appear, and Plex often needs a
**Refresh Metadata** on the show rather than a plain library scan.
