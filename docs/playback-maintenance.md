# Playback and maintenance

## After deploying this update

Back up persistent state and music before upgrading. Merge/rebuild/restart using
your normal deployment process; this PR itself does not deploy or modify your
running library. Schema remains v2. Existing v1 upgrades are backed up and atomic.

1. Select the intended library user and run **Verify library**. This reads current
   files and reports discovery, loudness, hash, beets and path issues.
2. Inspect Maintenance and choose **Repair eligible tags**. This covers all safely
   eligible audited tracks, not just the visible page. It queues serial, durable
   per-track jobs. Original encoded audio, identity and path are retained. A staged
   copy is verified before atomic replacement; interrupted jobs can be retried.
3. Rescan the library in Navidrome after repairs finish. Refresh/requeue playback so
   the player receives current gain and peak values, not an old queue snapshot.
4. In Navidrome personal playback settings select **Use Track Gain**, initially
   with ReplayGain preamp **0 dB**. The exact menu wording may vary by version.
   Album mode is not a substitute: this ingestor downloads individual recordings
   and does not invent album gain from one track.
5. Use **Retry all failed** for ingestion/processing errors. Deferred jobs already
   have automatic retries. Do not use redownload/reprocess merely to fix loudness.

## Why quiet files could stay quiet

The inspected Navidrome web player requires both gain and peak; if either is
missing it returns unity gain. The previous Opus configuration wrote R128 gain
without a peak. In addition, its nonstandard -18 LUFS R128 reference interacted
with Navidrome's +5 dB conversion. The updated path calculates fresh gain at the
standard -23 LUFS reference and measures true peak. Navidrome converts R128 Q7.8
using `/256 + 5` and uses peak to limit clipping. We avoid a conventional
REPLAYGAIN_TRACK_GAIN tag, which would take precedence over R128 in Navidrome.

Tag repair changes metadata, not the encoded audio packets. Loudness processing
is required for new/freshly processed audio; analysis failure is not silently
reported as a completed normalization. Existing files require audit/repair.
A quiet/loud synthetic pair and encoded-packet hashes are covered by regression
tests. Your specific low-volume recording and deployed client were not available
for end-to-end playback verification.

ReplayGain must be enabled in the player. Peak protection may reduce the requested
gain for dynamic recordings; matching integrated loudness does not mean identical
instantaneous volume. This implementation does not compress dynamics or re-encode
music to force equal volume. Do not compensate with a large global preamp.

## Discovery visibility

COMMENT and DESCRIPTION are both written as `Discovery Date: YYYY-MM-DD--HH-MM-SS
UTC`. The sample supplied for Regain Control already contained both values.
Use Music Ingestor > More > Technical details to see actual disk values and the
expected discovery date. Release dates remain separate MusicBrainz metadata.

The inspected Feishin development source labels its COMMENT column **Note**, with
that column disabled by default. Availability depends on the deployed version and
server connection; this is not a promise that older installations have that option.
Navidrome stores a comment field, but a universal configurable comment column in
all client versions is not assumed. Rescanning and client upgrades may be needed;
no Navidrome/Feishin source or server settings are changed by this repository.

## Safety and scale

Audit findings are paginated and filterable. Repair excludes unknown hash changes
and unreadable/outside-library files. Resolve these deliberately; never reset a
hash blindly merely to silence a warning. Hash mismatches caused by older app tag
writes can still need investigation. Repair does not retag identity, fetch artwork,
fetch lyrics, re-fingerprint or redownload. Reprocess is the separate full workflow.

Bulk approval is intentionally separate from bulk retry/repair: approving every
top candidate can accept a wrong match. Review uncertain matches, and use the
Unmatched or Needs approval filters. JSON failure export preserves error context
without copying configured API keys. Library user folders are not authentication;
keep the administration UI behind an authenticated reverse proxy.

## Primary references checked 2026-10-10

- [beets ReplayGain configuration](https://beets.readthedocs.io/en/stable/plugins/replaygain.html)
- [Pinned beets ReplayGain implementation](https://github.com/beetbox/beets/blob/v2.14.1/beetsplug/replaygain.py)
- [Ogg Opus R128 mapping, RFC 7845 section 5.2](https://www.rfc-editor.org/rfc/rfc7845.html#section-5.2)
- [Navidrome web gain/peak calculation](https://github.com/navidrome/navidrome/blob/497dfe25866d32e2229bcd962c38bda0e753c875/ui/src/utils/calculateReplayGain.js)
- [Navidrome metadata conversion](https://github.com/navidrome/navidrome/blob/497dfe25866d32e2229bcd962c38bda0e753c875/model/metadata/map_mediafile.go)
- [Feishin column definitions](https://github.com/jeffvli/feishin/blob/9dc15477c86fbd48d1a98fa878769d09811895e8/src/renderer/components/item-list/item-table-list/default-columns.ts)
