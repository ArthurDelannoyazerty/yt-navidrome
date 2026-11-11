# Interface decisions

The primary task is importing and reviewing music. The Music screen uses
quick filters, one search field, one status selector, and a compact track list.
Source URLs expand in place; repair, redownload and destructive actions live in
More. Administration explanations belong in Help. Pagination is repeated below
the list, and both controls share the same server-side filtering and page state.

The visual system uses neutral surfaces, a single green action color, restrained
status colors, consistent spacing, labelled controls and visible keyboard focus.
On narrow screens the music table becomes stacked rows. Help combines a Pele
flowchart, numbered steps, recovery guidance and an expandable transition table.
Only the pinned flowchart renderer is loaded, locally, when Help is opened.

Sources consulted:

- [Nielsen Norman Group: Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [GOV.UK Design System: Pagination](https://design-system.service.gov.uk/components/pagination/)


## Focused music controls

Music includes an **Ignored music** view beside the usual quick filters. It has
search and pagination, and keeps separate controls for allowing a recording,
restoring one URL, or restoring all rules. Library totals count active music.

The track **More → Remove…** dialog explains removal and ignore scope. It shows
only distinct scopes supported by the selected entry; blocking a recording is
explicit. **Review metadata → Original source metadata** remains the single place
to restore retained original tags. Advanced editing and audio tools remain in More.

Activity follows the latest message by default, including when logs arrive before
the tab is opened. Scrolling up pauses following; **Jump to latest** resumes it.
The Help guide separates the usual workflow from temporary failures and includes
an expandable table of every documented operation transition.

## Approval page

Approval is a full-width application panel between Music and Sources. It is not
a dialog. A desktop two-column grid keeps the current track on the left and
matching controls on the right. Beets suggestions are table rows, the source-tag
choice is a separate button, and MusicBrainz search is visible and automatic.
Both result tables place **Use** in the first column and show separate track,
artist and album columns. Only the button applies a suggestion; clicking or
selecting text elsewhere in the row does not change metadata. The results take
slightly more of the desktop width, with the current track still visible on the
left. Narrow screens scroll the tables horizontally while keeping actions in
view. MusicBrainz album names come from the returned releases; selecting an
edition shows its album in that column. The MusicBrainz search selector, title,
artist and Search button share a baseline.
Previous/Next and a collapsed, searchable, paginated title/channel picker replace
the old Music review toolbar. The Show options match Music but default to
Waiting for approval independently. No custom keyboard shortcuts are introduced.

Both views include **Imported from YouTube** and **Imported from Spotify** filters.
They use retained discovery provenance; a Spotify import's YouTube audio candidate
does not turn it into a direct YouTube import. Origin details retain both URLs,
without introducing another badge into the main music list.
