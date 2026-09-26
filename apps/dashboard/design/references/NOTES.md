# Reference compositions: analysis notes

Static compositions built from `apps/dashboard/DESIGN.md` tokens
(`tokens.css`, `*.html`), rendered with Playwright CLI at 1440×900 and
390×844 (`screenshots/`), then analyzed before implementing
(docs/frontend/design-workflow.md, step 2). Data shapes are the real API's.

## What works (keep)
- Dark Carbon UI-shell header, white canvas, 1px hairline panels, no shadows,
  square corners: reads as an operations console, not a marketing page.
- Status as a small colored square inside a neutral gray tag: color carries
  meaning without flooding the screen (error red, warning yellow, success
  green, info blue only).
- Plex Mono for ids, hashes, versions, timestamps; 8-char short ids with the
  full id available.
- Detail layout at desktop: timeline + impact on the left, the lifecycle
  (RCA → hypotheses → remediation → verification) on the right.
- Timeline rows: mono time, thin rule, one-line event; RCA/verification rows
  marked by a blue rule, failures by red.

## Problems found (fix in implementation)
1. Detail at 390px: the two columns never collapse -- text crushes, panels
   overflow. → single column below 1056px (Carbon `lg`).
2. Incident id wraps across lines in the header. → short id + copyable full id.
3. List at 390px overflows horizontally. → priority columns only below
   1056px; below 672px each incident becomes a stacked row (id, status,
   service/env, opened), no horizontal page scroll.
4. The current lifecycle step isn't obvious, and an approval (the one thing an
   operator may need to *do*) would be buried in the remediation panel. →
   a lifecycle progress strip under the header, and an "action needed" panel
   right under it when a remediation awaits approval.
5. Verification sits below the fold on desktop. → right column ordered by
   the lifecycle; verification shows baseline → observed values as a compact
   strip plus the per-observation table.
6. Overview tiles are 6-up at every width. → 3-up at tablet, 2-up on phone.

## Decisions
- Implement with the official Carbon React components (`@carbon/react`):
  UI shell header, DataTable, Tag, ProgressIndicator, Select/DatePicker-free
  filters (Select + time-range Select), Modal for evidence and approval,
  InlineNotification for errors, DataTableSkeleton/SkeletonText for loading.
- No hero, no charts that aren't backed by recorded data, no animation beyond
  Carbon's own component transitions.
