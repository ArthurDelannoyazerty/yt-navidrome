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
