# Frontend design workflow (for Phase 8)

Set up before Phase 8; no dashboard code exists yet. This document says
which design/browser tools the project uses, where they live, how each is
used, and the one visual direction the dashboard follows.

## Tooling

| Tool | Where | Pinned by | Role in Phase 8 |
|---|---|---|---|
| **Taste** (`design-taste-frontend`) | `.claude/skills/design-taste-frontend/` (project-local) | `skills-lock.json` → `Leonxlnx/taste-skill` | implementation guardrails (see limits below) |
| **Image-to-Code** (`image-to-code`) | `.claude/skills/image-to-code/` | `skills-lock.json` → `Leonxlnx/taste-skill` | reference-first design of visually important screens |
| **Vercel Web Design Guidelines** (`web-design-guidelines`) | `.claude/skills/web-design-guidelines/` | `skills-lock.json` → `vercel-labs/agent-skills` | final audit; fetches the live rules from `vercel-labs/web-interface-guidelines` each run |
| **Playwright CLI** (`playwright-cli`) | `.claude/skills/playwright-cli/` (skill) + global `@playwright/cli` 0.1.21 (`/opt/homebrew/bin/playwright-cli`) | npm global install; the skill was written by `playwright-cli install --skills` (not in `skills-lock.json`) | drive the running UI: snapshots, interactions, viewports, screenshots |
| **awesome-design-md** | not installed -- one file copied: `apps/dashboard/DESIGN.md` | upstream path `design-md/ibm/DESIGN.md` @ `VoltAgent/awesome-design-md` commit `e06a966`, blob `dbef1c5` (MIT) | the visual source of truth |

Claude Code discovers the four skills from `.claude/skills/` automatically
(no settings needed). Node is v22.12.0: satisfies `@playwright/cli`
(>= 18), Next.js 16 (>= 20.9) and Vite 8 (>= 22.12.0 -- exactly at the
floor). Playwright CLI writes to `.playwright-cli/` in the working
directory (snapshots, console logs, screenshots); it is gitignored because
it can capture credentials.

To restore on another machine:

```bash
npx skills experimental_install                 # the three skills pinned in skills-lock.json
npm install -g @playwright/cli@0.1.21           # the CLI
playwright-cli install --skills                 # its skill, into .claude/skills/ (not in the lock)
```

## Visual direction: IBM / Carbon (`apps/dashboard/DESIGN.md`)

Chosen from the 73 systems in awesome-design-md for a production incident
command center:

- **Color carries meaning, not brand.** One interactive accent (blue:
  links, primary actions, focus) and a full semantic set (red / yellow /
  green). In an incident console, color has to mean severity and state;
  a brand palette that competes with that is a safety problem.
- **Built for density.** Flat square tiles, 1px hairlines, surface
  changes instead of shadows: tables, timelines and evidence lists read as
  structure rather than decoration.
- **Plex Sans + Plex Mono.** Ids, hashes, versions and evidence ids need a
  first-class monospace.
- **An official implementation exists.** `@carbon/react` + `@carbon/styles`
  (Apache-2.0) supplies the data table, accessible components and the Gray
  100 dark theme the DESIGN.md itself lists under "Known Gaps".
- **Accessibility lineage** (contrast, focus, 48px touch targets).

Not chosen: ClickHouse (yellow brand accent collides with "warning"),
HashiCorp (multi-product accent palette collides with status colors),
Sentry (playful illustrated personality), SpaceX (image-led, little
information structure), Linear / Warp (well-crafted, but generic
dark-SaaS / terminal marketing chrome with no semantic color system).

### Rules for applying it to an operations console

The DESIGN.md describes IBM's *marketing* pages. The dashboard applies it
with these explicit, minimal adaptations (anything else is drift):

1. Use Carbon's official packages; do not hand-recreate its CSS (Taste's
   "honesty rule"). One design system in the tree.
2. Operational screens use Carbon's productive type set and compact
   density; the 300-weight display sizes and the marketing page rhythm
   (hero, logo marquee, newsletter band) do not appear.
3. Severity and status map only to the semantic tokens (critical/failed →
   error red, warning/awaiting approval → warning yellow, resolved/executed
   → success green, informational → blue). Blue is never decoration.
4. Square corners, hairlines, no shadows, no gradients, no glass, no
   mascots, sentence case.
5. A dark theme, if offered, is Carbon's Gray 100 theme (built from the
   DESIGN.md's inverse tokens), not an invented palette; the theme is
   locked per page.
6. No IBM name, logo or trade dress in the product -- the system is the
   inspiration, the brand is ours.

## Workflow

```
DESIGN REFERENCE → IMAGE-TO-CODE → TASTE IMPLEMENTATION
    → PLAYWRIGHT VALIDATION → VERCEL AUDIT → REFINEMENT (loop to validation)
```

### 1. Design reference
Read `apps/dashboard/DESIGN.md` and the relevant Carbon component docs.
State the design read in one line (Taste §0.B), e.g. *"Operations console
for on-call engineers, Carbon productive language, dense, semantic color
only."*

### 2. Image-to-Code (before any visually important screen)
The skill is image-first: reference image → deep analysis → code.
**Claude Code has no image-generation tool**, so the reference is produced
the other way round:
1. build a static reference mock of the screen from DESIGN.md tokens and
   realistic data shapes from the API (`/api/v1/...`), no framework;
2. render it with Playwright CLI at 1440×900 and 390×844 and save the
   screenshots under `apps/dashboard/design/references/`;
3. analyze the screenshots per the skill (text, type, spacing, component,
   color extraction; anti-nesting; clutter reduction) and write the
   extraction notes next to them;
4. implement against the images and notes.
If the user supplies images (sketches, Stitch output), those replace step 1.
The skill's defaults are tuned for marketing sites (density 3/10,
generous spacing, hero rules); for this console, density is higher and
there is no hero.

Visually important screens (candidates, not a scope commitment): the
incident queue, incident detail (alerts, investigation timeline, RCA with
evidence), the remediation approval view (proposal, policy decision and
rules, exact hash being approved), and kill switches.

### 3. Taste implementation
Taste declares dashboards and data tables out of its own scope and points
them at Carbon (§2.A, §13). Use it for what transfers: brief inference,
dependency verification before any import (§3.F), interactive states
(§4.5), layout discipline and explicit mobile collapse (§4.7), theme lock
(§4.11), reduced motion (§6.B), and the AI-tells list (§9) -- no fake
"Jane Doe" data, no div-drawn fake UI, no em-dash copy. Tables use
Carbon's DataTable.

### 4. Playwright validation (against the running UI)
```bash
playwright-cli open http://localhost:<port>/incidents
playwright-cli snapshot                      # element refs for interaction
playwright-cli click <ref>                   # e.g. open an incident, start an approval
playwright-cli resize 1440 900 && playwright-cli screenshot --filename=incidents-1440.png
playwright-cli resize 390 844  && playwright-cli screenshot --filename=incidents-390.png
playwright-cli console                       # no errors
playwright-cli close
```
Exercise the real flows (open incident → review RCA → approve / reject a
remediation with its hash → kill switch confirm), check the Carbon
breakpoints (1584 / 1312 / 1056 / 672 / 320), keyboard focus and console
errors, and compare against the reference images. Use a local operator
token only; `file://` is blocked, so serve over HTTP.

### 5. Vercel Web Design Guidelines audit
Run the `web-design-guidelines` skill on the dashboard sources (e.g.
`apps/dashboard/src/**/*.tsx`). It fetches the current rules at run time
and reports `file:line` findings.

### 6. Refinement
Fix findings, re-run steps 4-5 until clean, and record deliberate
deviations (with reasons) in this document.

## Phase 8: as executed

1. **References.** Static compositions in Carbon tokens
   (`apps/dashboard/design/references/*.html`, `tokens.css`) for the list,
   detail and overview; screenshotted at 1440 and 390 with playwright-cli;
   notes in `design/references/NOTES.md`.
2. **Implementation.** `@carbon/react` components (Header, DataTable,
   Select, ProgressIndicator, Modal, Toggle, InlineNotification, skeletons)
   plus a small `ii-*` layout layer in `src/styles.scss`. Taste was applied
   only where it transfers (dependency checks, interaction states, mobile
   collapse, no fake data); it was not forced onto the console.
3. **Playwright validation.** Iteration 1 (`design/screenshots/iter1-*`)
   found: light header (→ `Theme g100` shell), doubled top padding, tag
   label casing ("Rca ready"), status tags misused as metadata, header
   navigation lost below 1056 px (→ HeaderMenuButton + SideNav), lifecycle
   stepper overflowing on mobile (→ compact text form under 672 px).
   The Playwright suite then found two accessibility bugs: Escape did not
   close dialogs on the first press (focus landed on the close button,
   whose tooltip consumed it → initial focus moved into the content) and
   horizontally scrolling tables were not keyboard-reachable (→ focusable
   labelled scroll regions). Final captures: `design/screenshots/final-*`.
4. **Guidelines audit.** The `web-design-guidelines` skill (Vercel Web
   Interface Guidelines, fetched at run time) over `src/**/*.tsx`. Fixed:
   hand-built time formatting (→ `Intl.DateTimeFormat`, UTC), `Loading` →
   `Loading…`, approval inputs without `name` / `spellCheck={false}`,
   an unconditional `outline: none` (→ only when not `:focus-visible`),
   `touch-action: manipulation`, tabular numerals, `content-visibility` on
   long timelines. Already satisfied: labelled controls and icon buttons,
   skip link, heading order, URL-held filters, links for navigation,
   buttons for actions, `theme-color`, no zoom blocking, errors with retry.

Deliberate deviations: sentence case, not Title Case (Carbon / IBM
convention in `DESIGN.md`); times in UTC for every responder; the approval
submit stays disabled until token and approver are filled (Carbon modal
pattern); light theme only (the console is a work surface; a
dark theme is a Carbon theme switch away); dense tables scroll inside
labelled regions on phones rather than dropping columns that carry evidence.
