# Interface decisions

The primary task is importing and reviewing music. The Music screen uses four
quick filters, one search field, one status selector, and a compact track list.
Source URLs expand in place; repair, redownload and destructive actions live in
More. Administration explanations belong in Help. Pagination is repeated below
the list, and both controls share the same server-side filtering and page state.

The visual system uses neutral surfaces, a single green action color, restrained
status colors, consistent spacing, labelled controls and visible keyboard focus.
On narrow screens the music table becomes stacked rows. Help includes an inline,
accessible SVG state diagram with text alternatives, without a CDN dependency.

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
