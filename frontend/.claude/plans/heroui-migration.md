# Plan: HeroUI v3 Migration (Pilot = AskPage)

## Decisions (locked)

| Topic      | Choice                                                                             |
| ---------- | ---------------------------------------------------------------------------------- |
| Library    | **HeroUI v3** (official path for new projects; v2 is maintenance-only)             |
| CSS engine | **Tailwind CSS v4** (CSS-first, `@tailwindcss/vite`, `hero.ts`)                    |
| Look       | **HeroUI default appearance** (do NOT remap the current mint/amber/Fraunces brand) |
| Scope      | **Pilot one page first** — migrate AskPage, then decide on SimPage                 |
| Theme      | **Light + dark** (HeroUI light/dark themes + a theme switch)                       |
| Pilot page | **AskPage** (问答页)                                                               |

## Goal

Wire in the HeroUI v3 + Tailwind v4 infrastructure **without breaking the
existing simulator page (SimPage)**, and fully migrate AskPage to the HeroUI
default look with a light/dark switch. Validate on the pilot, then decide
whether to roll out to SimPage.

## Current state (context)

- Vite + React 18 + TS + react-router. **No Tailwind, no component library** —
  all hand-written CSS: `src/index.css` (508 lines) + `src/sim.css` (673 lines).
- Custom brand today (will be replaced by HeroUI default on AskPage): warm paper
  bg + mint `#1f8a6d` + amber `#c8743a`, Fraunces (serif display) + Plus Jakarta
  Sans, custom radius/shadow tokens.
- Pages: `src/pages/AskPage.tsx` (pilot) and `src/pages/SimPage.tsx` (untouched
  this round — drag-drop timetable, rules pane, program search).

## Build-time findings (2026-06-11, verified against npm + heroui.com/llms-full.txt)

Facts that supersede assumptions made when this plan was written:

1. **HeroUI v3 is `latest` on npm (3.1.0)** — not beta. Peer deps:
   `react >= 19`, `react-dom >= 19`, `tailwindcss >= 4`.
2. **React 19 upgrade is mandatory** — project was on React 18.3.1. App is
   small, already on `createRoot`, no legacy APIs; react-router 7 supports 19.
   Upgrade `react`, `react-dom`, `@types/react`, `@types/react-dom` to 19.
3. **No `hero.ts`, no `@plugin`, no `@custom-variant`** — that was v2.8/beta
   syntax. v3 setup is two CSS lines (order matters):
   `@import "tailwindcss";` then `@import "@heroui/styles";`
   Packages: `npm i @heroui/styles @heroui/react`.
4. **No `HeroUIProvider`** — v3 is CSS-driven, components need no provider.
5. **framer-motion is NOT a v3 dependency** (beta docs said 11.9+; release uses
   React Aria + CSS). For the plan's Motion section install **`motion`**
   ourselves; import from `motion/react`.
6. **Dark mode** = `.dark` class / `data-theme="dark"` on `<html>`; light is
   `:root` default. For Vite apps use the **`useTheme` hook from
   `@heroui/react`** (localStorage + system resolution built in). Keep
   `bg-background text-foreground` on the app shell.
7. **Component renames vs plan mapping**: no Navbar in v3 (build top nav with
   Tailwind + NavLink), Progress → **ProgressBar**, Divider → **Separator**.
   Autocomplete / NumberField / Alert / Toast / Tabs / RadioGroup / Skeleton /
   Spinner / Tooltip / Chip / Card all confirmed present.
8. **Docs for agents**: `https://www.heroui.com/react/llms.txt` (index),
   `/llms-full.txt` (complete, ~6.8MB) — fetch sections by grep, the HTML site
   is JS-rendered.

## Phase 0 — Infrastructure (no visual change to any page)

1. Install deps with an **absolute npm path** (avoid the nvm lazy-load recursion
   in non-interactive shells): React 19 upgrade (`react`, `react-dom`,
   `@types/react`, `@types/react-dom`) + `tailwindcss`, `@tailwindcss/vite`,
   `@heroui/styles`, `@heroui/react`, `motion`.
2. `vite.config.ts`: add the `@tailwindcss/vite` plugin.
3. New `src/styles/tailwind.css`: `@import "tailwindcss";` +
   `@import "@heroui/styles";`. Import it in `main.tsx` **before** the existing
   `index.css` / `sim.css` — unlayered legacy CSS has higher cascade priority
   than Tailwind's `@layer base`, so `sim.css` is not clobbered by preflight.
4. No provider changes (v3 needs none).
5. Checkpoint: `npm run dev` + `npm run build` pass; SimPage screenshot shows no
   regression.

## Phase 1 — Migrate AskPage

1. Read the rest of `index.css`; classify "AskPage-only" classes
   (searchcard / chips / skeleton / badge / note ...) vs "shared" classes
   (topnav).
2. Top nav: rebuild `App.tsx` topnav with HeroUI **Navbar** (shared by both
   pages, neutral). Add a **ThemeSwitch** (light/dark).
3. Rewrite AskPage with HeroUI defaults: **Input** (with search icon) +
   **Button** (loading state) + **Chip** (examples) + **Card** +
   **Skeleton / Spinner**, plus Tailwind utilities for layout/typography.
4. `Results` component: rebuild with HeroUI Card / Chip / Divider.
5. Remove the replaced **AskPage-only** CSS (verify no other references before
   deleting each class). `sim.css` and SimPage stay untouched.
6. Dark mode: ThemeSwitch toggles `.dark` on `<html>`. **Pilot only fully
   themes AskPage**; reset to light when leaving AskPage so SimPage's legacy CSS
   does not break under dark. SimPage dark mode comes with its later migration.
7. Checkpoint after each step; roll back to last checkpoint on failure.

## Phase 2 — Verify & hand off

1. dev / build / lint all pass.
2. Screenshots: AskPage light + dark, 375px width, reduced-motion; SimPage no
   regression.
3. Pilot summary → user decides whether to roll out to SimPage.

## Pilot result (2026-06-11) — Phases 0–2 DONE ✅

Verified: build + lint clean, 0 console errors; AskPage light/dark/375px/
reduced-motion all pass; SimPage pixel-faithful (placed-chip color asserted
equal to pre-migration `--ink-soft`).

Implementation notes that Phase 3 must know:

1. **Scoped dark, not global** — pilot theme state lives in `App.tsx`
   (`uq-theme` in localStorage); `.dark` + `bg-background` go on the app shell
   div ONLY on the ask route, so SimPage can never render dark. The toggle is
   hidden on `/sim`. Phase 3: replace with HeroUI's global `useTheme` and
   delete the scoped logic. Caveat: portal components (Toast/Modal/Tooltip)
   render outside the shell and would NOT inherit scoped dark — fine in the
   pilot (none used on AskPage), but Phase 3's toasts make global `useTheme`
   mandatory.
2. **Cascade-layer trap (bit us twice)** — unlayered legacy CSS beats ALL
   Tailwind/HeroUI layered rules, even single-element selectors vs utility
   classes. Fixes applied: legacy `header` / `footer` / `h1` / `h1 em` rules are
   now scoped under `.simpage`; legacy `.chip` / `.skeleton` deleted because
   they collide with HeroUI's BEM classes (`.chip`, `.skeleton`). A
   `.simpage .chip { color; transition }` shim preserves the timetable chip
   look (sim.css's nested `.chip` relied on the deleted rule's cascade).
   Phase 3 watch-list: legacy `.badge` still exists unscoped and WILL pollute
   HeroUI `Badge` if one is ever rendered; same for `.note` (no HeroUI
   collision today).
3. **Card flex gotcha** — Card parts are flex containers; inline children like
   `<code>` become stretched flex items. Wrap text content in a `<div>` inside
   `Card.Content`.
4. **index.css is now ~120 lines**: tokens + body + SimPage-shared legacy
   (`.wrap`, `.simpage header/footer`, `.badge`, `.dot`, `.simpage h1`, `.sub`,
   `.note`, chip shim, reduced-motion). All AskPage/Results legacy CSS deleted.
5. Files: `src/lib/motion.ts` (rise variants + easeOut + riseDelay),
   `src/App.tsx` (nav + theme), `src/pages/AskPage.tsx`,
   `src/components/Results.tsx` — all HeroUI v3 + Tailwind utilities + motion
   with `useReducedMotion` fallbacks.

## Phase 3 — Migrate SimPage (rollout, after pilot sign-off)

HeroUI infra from Phase 0 is reused. SimPage is the larger surface: drag-and-drop
timetable, rules/progress pane, program autocomplete.

**Hard constraint — native drag-and-drop stays.** HeroUI has no DnD primitive.
The DnD plumbing in `src/lib/sim.ts` (`setDragCode` / `getDragCode` /
`dataTransfer`) and the `dragstart` / `dragover` / `drop` handlers are unchanged.
We only restyle the draggable cards / cells / chips with HeroUI + Tailwind.

### Component mapping (legacy class → HeroUI v3)

| Surface                               | Legacy class                                              | HeroUI v3                                                                                |
| ------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Header badge / title / sub            | `.badge` `h1` `.sub`                                      | Chip + typography (same as AskPage)                                                      |
| Program search                        | `.search` `.drop` `.opt` `.curprog` + manual keyboard nav | **Autocomplete** (built-in keyboard nav + filtering)                                     |
| Pane container                        | `.pane` `.panehead`                                       | Card + section header                                                                    |
| Jump-to-course bar                    | `.csearch.jumpbar` `.jumpdrop`                            | **Autocomplete** (or Input + Listbox)                                                    |
| Branch toggle (二选一)                | `.brtoggle` `.brpill`                                     | **Tabs** or selectable **Chip** group                                                    |
| Plan options                          | `.major` `.radio`                                         | **RadioGroup / Radio**                                                                   |
| Rule section                          | `.rulesec` `.rulehead` `.runits`                          | Card + header row                                                                        |
| Progress bar                          | `.pbar` `i` (+`.over`)                                    | **Progress** (primary; danger when over)                                                 |
| Course card (draggable)               | `.ccard` `.ccode` `.ctitle`                               | Card keeping `draggable` + `onDragStart`; **Chip** for offerings/lock tags               |
| Equiv "二选一" card                   | `.ccard.equiv` `.or`                                      | Card with inner option rows                                                              |
| Course search                         | `.csearch` + result list                                  | Input + result list (Card rows)                                                          |
| Unattributed warning                  | `.unatt`                                                  | **Alert** (warning)                                                                      |
| Timetable toolbar                     | `.ttbar` (select / number / btn)                          | **Select** (start_sem) + number **Input/NumberInput** + **Button**                       |
| Status line                           | `.overall` `.ok-txt` `.bad-txt` `.warn-txt`               | **Chip**s mapped to success / danger / warning                                           |
| Grid cells (drop zones)               | `.ttgrid` `.ttcell`                                       | CSS grid + native drop handlers; Tailwind styling; units **Chip**                        |
| Placed course (draggable + removable) | `.chip` `.ct` `.x` `.badwhy`                              | draggable wrapper + **Chip** (`onClose`=remove, danger when bad); reason via **Tooltip** |
| Toast                                 | custom `.toast` + timer                                   | HeroUI **toast** (`addToast` + `ToastProvider`) — drop the manual timer                  |
| Notes / errors                        | `.note` `.err`                                            | **Alert**                                                                                |

### Steps

1. Confirm each needed v3 component exists/stable (Autocomplete, Select,
   NumberInput, Progress, RadioGroup, Tabs, Alert, Tooltip, toast). Swap for an
   available alternative where missing.
2. `ProgramSearch.tsx` → Autocomplete (delete the manual keyboard-nav and
   outside-click effect).
3. RulesPane: rule sections (Card), progress (Progress), course cards
   (Card + Chip, **keep DnD attrs**), branch toggle (Tabs/Chip), plan options
   (RadioGroup), jump bar + course search (Autocomplete / Input).
4. Timetable: toolbar (Select / NumberInput / Button), status (Chip), grid cells
   keep native DnD + Tailwind styling, placed chips (Chip `onClose` + Tooltip).
5. Replace the custom toast with HeroUI toast; add `<ToastProvider>`.
6. Map status colors (green / red / amber) to HeroUI semantic tokens
   (success / danger / warning) so light + dark both pass contrast. Enable
   SimPage under `.dark` and remove the Phase-1 "reset to light on leave" guard.
7. Delete the migrated `sim.css` classes (verify no other references before each
   delete). Keep only grid rules HeroUI cannot express.
8. Checkpoint after each component; screenshots light + dark + 375px; re-test the
   full DnD flow: drop-block on wrong semester, cap-over, remove, auto-place,
   clear.

## Phase 3 result (2026-06-11) — DONE ✅

Both pages are now fully HeroUI v3; zero legacy CSS imported. Verified by build +
lint + Playwright: click-place, native drag-drop (synthetic DragEvent +
DataTransfer through the real dragstart→dragover→drop path), blocked drop on a
wrong-semester cell fires the HeroUI toast, auto-schedule, remove, clear,
S1/S2 switch relabels cells, dark mode, 375px single-column, AskPage regression
— all pass, 0 console errors.

Mapping deviations vs the plan table (all justified):

1. **ComboBox, not Autocomplete** — v3's Autocomplete is a trigger+popover
   select; ComboBox (input + filtered listbox) matches the existing
   type-to-search UX for both the program search and the jump bar.
2. **Plan options use ToggleButtonGroup, not RadioGroup** — RadioGroup cannot
   deselect by clicking the selected item; the legacy `setPlan` toggles off.
   Single-select ToggleButtonGroup without `disallowEmptySelection` preserves
   that. Branch toggle (二选一) uses ToggleButtonGroup WITH
   `disallowEmptySelection`.
3. **Conflict reasons stay inline** (small danger text under the chip), not a
   Tooltip — hover-only info fails on touch and the legacy UI showed it inline.
4. **Draggable cards/chips are plain divs + Tailwind tokens**, not Card/Chip
   roots, so native DnD attrs can't be swallowed; HeroUI Chips are used for the
   non-draggable bits inside (code, offerings, locks, status).
5. **NumberField needs explicit input width** (`NumberField.Input
className="w-14"`) and `formatOptions={{useGrouping:false}}` for years —
   otherwise 2026 renders as "2,026" and squeezed containers clip the value.

Cleanup state: `index.css` and `sim.css` are emptied stubs (rm is not allowed
in this environment) — `git rm` them at will; imports already removed.
Google-Fonts links dropped from index.html (Fraunces/Jakarta unused). The only
custom CSS left lives in `src/styles/tailwind.css`: html background token,
`.flash` jump-highlight keyframes, reduced-motion kill-switch.

Notes:

- Deleting legacy `:root { --radius: 18px; --shadow… }` un-polluted HeroUI's
  same-named tokens — field/button radii on BOTH pages are now true HeroUI
  defaults (slightly rounder than the pilot screenshots).
- Theme is global `useTheme('light')` from @heroui/react (localStorage +
  `.dark`/`data-theme` on `<html>`); the pilot's scoped-dark shell and
  `uq-theme` key are gone. `<Toast.Provider />` mounts once in App.
- Bundle: 225KB gzip JS (RAC pickers/toast machinery). If it matters later:
  route-level code-split or granular `@heroui/*` imports — not done, not asked.

## Motion (Framer Motion) — cross-cutting (Phase 1 + Phase 3)

`framer-motion` is already a hard dependency of HeroUI v3 (11.9+) — no extra
install. HeroUI components animate their own open/close (dropdown, autocomplete,
modal, toast) internally; **do not wrap those in extra motion**. Hand-author
motion only for the custom page-level / layout moments below.

> Import note: framer-motion 11+ also ships as `motion`
> (`import { motion, AnimatePresence } from "motion/react"`). Use whatever
> HeroUI v3 pins and align the import path with it.

### Where (high-impact only — 1–2 animated elements per view)

- **AskPage hero load (Phase 1)**: staggered reveal badge → h1 → sub → search
  card → chips (opacity + small translateY, ease-out, ~40ms stagger).
- **Ask results (Phase 1)**: `AnimatePresence` crossfade skeleton → results;
  stagger result cards in (~30–50ms). Exit faster than enter.
- **Route transition AskPage ↔ SimPage (Phase 1)**: subtle directional
  fade/slide via `AnimatePresence` on the router outlet.
- **SimPage placed course (Phase 3)**: animate the chip's mount into a cell
  (scale 0.96→1 + fade) and removal (`AnimatePresence` exit). Use the `layout`
  prop for reflow when a cell's chips change — transform-based, no CLS.
- **SimPage progress (Phase 3)**: HeroUI Progress already animates its fill — do
  not re-animate; hand-author only if a custom bar is kept.

### Rules (from ui-ux-pro-max animation domain)

- transform / opacity only — never animate width / height / top / left.
- Enter 200–300ms ease-out; exit ~60–70% of that, ease-in. Prefer spring for the
  placed-chip mount.
- Stagger 30–50ms per item; cap to 1–2 animated elements per view.
- **Respect reduced motion**: gate every hand-authored animation behind
  `useReducedMotion()` — fall back to instant / opacity-only.
- Centralize presets in one `src/lib/motion.ts` (shared `variants` + `transition`
  tokens) so the rhythm is unified — no ad-hoc durations per file.

### Do NOT

- Do not use framer-motion `drag` / `whileDrag` for the timetable — it conflicts
  with the native HTML5 DnD. Drag feedback stays CSS on the native drag; framer
  only handles mount / unmount / `layout` of the cards.
- Do not animate a HeroUI component's built-in open/close a second time.

## Risks & tradeoffs (explicit)

- **Tailwind v4 preflight (global reset)**: buffered by "unlayered legacy CSS
  wins the cascade", but it can still change defaults the legacy CSS does not
  set explicitly (button appearance, list margins, etc.). Fallback if SimPage
  regresses: narrow the preflight scope.
- **Dark mode covers the pilot page only**; full-app dark waits for SimPage.
- **v3 is new**: if a needed component is missing/unstable in v3, swap for an
  available alternative or keep the bespoke piece.
- **One item to confirm at build time**: the exact v3 CSS directives
  (`@plugin` / `@source` / `@custom-variant` + `hero.ts` contents) — the docs
  site is JS-rendered and could not be scraped in full; verify verbatim against
  the official page while implementing.

### SimPage-specific (Phase 3)

- **Drag-and-drop is native, not HeroUI** — restyling must not break
  `dragstart` / `dragover` / `drop` or the `dataTransfer` / `getDragCode`
  plumbing in `src/lib/sim.ts`; re-test the full DnD flow after restyle.
- **HeroUI Chip may not forward `draggable` / `onDragStart` cleanly** — if it
  swallows them, wrap a draggable element around/inside the Chip, or keep a
  styled custom chip for placed courses.
- **v3 component availability** — Autocomplete / NumberInput / Alert / toast must
  be confirmed present in v3; fall back if any is missing.
- **Status color semantics** — move `ok` / `bad` / `warn` off hardcoded hex onto
  success / danger / warning tokens, or dark-mode contrast breaks.

### Motion (Framer Motion)

- **Reduced motion** — every hand-authored animation needs a `useReducedMotion()`
  fallback, or it fails the a11y check.
- **framer `drag` vs native DnD** — keep them separate (Phase 3); mixing them
  breaks the timetable drop logic.

## Out of scope (do not touch)

Backend, the API layer (`src/api/`), and the DnD core in `src/lib/sim.ts`
(`setDragCode` / `getDragCode` — restyle around it, don't rewrite it).

SimPage is out of scope for the pilot (Phase 0–2) but is migrated in **Phase 3**.
