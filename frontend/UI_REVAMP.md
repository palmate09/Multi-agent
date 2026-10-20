# UI revamp — what changed and why

This is a front-end-only change. No API, route, type or backend contract was
touched, and **no dependency was added** (the Docker build runs `npm ci`, so the
lockfile is unchanged).

## Themes

|          | Dark ("graphite")      | Light ("daylight")      |
| -------- | ---------------------- | ----------------------- |
| Surfaces | neutral charcoal       | white on cool grey      |
| Accent   | bright sky, dark label | deeper sky, white label |

The old navy-and-indigo theme is replaced. A new **System / Light / Dark** switch
sits in the top bar and on the sign-in page. The choice is remembered, follows
the OS while on _System_, and is applied by a small inline script in
`index.html` before first paint, so a light-mode user never sees a dark flash.

Both themes live in `src/styles/tokens.css`. Every colour pair is annotated with
its measured contrast and passes WCAG AA — verified again in the real rendered
DOM (see _Verification_).

## UX changes

1. **Master/detail layout.** The "New run" form used to sit above every run and
   push the results below the fold. Now the sidebar has a **New run** button;
   the main area shows the form (with a "how a run works" overview) _or_ the run.
2. **Pipeline stage rail** in every run header: all stages in one line, with
   done / running / failed / waiting, so "where is my run, and where did it
   stop?" is visible from any tab.
3. **The requirement is shown in the run header.** Runs were identified only by id.
4. **Stats strip** with the verdict coloured; **advisories** side by side.
5. **Grouped tabs** (watch · design · build · ship) with count pills.
6. **Timeline** shows time, per-stage duration, retries as chips, and upcoming
   stages while a run is moving. Fixes a stray "Pipeline" row after "Final".
7. **Code viewer**: toolbar, line numbers, syntax colouring, **Copy**.
8. **Sidebar**: search, status filters, hover-revealed delete, system status
   footer. Mobile drawer: Escape and backdrop close it, and its links are out of
   the tab order while closed.
9. **Composer**: example chips with real names, pipeline options in a
   disclosure that says how many changed, **Ctrl/⌘+Enter** to run.
10. **Sign-in**: says what the product does; show/hide password.
11. **Playground**: browser-style frame with address bar and "open in new tab".
12. Icons replace text glyphs; nothing below 11px; labels are sentence case.

## Bugs fixed along the way (all present in the original)

- **Playground polled once.** The effect depended on `status.state`, which does
  not change while still `starting`, so a slow boot left the badge on
  "starting" forever. Your own e2e test for this failed on the original build.
- **Text inputs were unstyled.** `<input>` had no `type`, so `input[type='text']`
  never matched. `Input` now defaults to `type="text"`.
- **"Write a requirement" focused `#req`, which did not exist.**
- **Subtitle rendered twice** in the top bar.
- **Timeline showed a "Pipeline" row after "Final"**, and "Final" and "Finished"
  duplicated each other.
- **Duplicate character count** next to the form's submit button.

## Files

New: `lib/theme.ts`, `lib/highlight.ts`, `lib/pipeline.ts`, `lib/agents.ts`;
`components/RunHeader`, `PipelineRail`, `TeamOverview`;
`components/ui/Icon`, `CodeBlock`, `ThemeSwitch`.
Rewritten: `styles/tokens.css`, `styles.css`, `index.html`, `main.tsx`,
`RunList`, `RequirementForm`, `LoginPage`, `PipelineTimeline`, `FileViewer`,
`UiPanel`, `PlaygroundPanel`. Edited: `App.tsx` and the `ui/` primitives (APIs
preserved; each gained only optional props).
New tests sit beside the code in `__tests__/`.

## Before you merge

- Run `npm install && npm run lint && npm run typecheck && npm run test && npm run format`.
  I could not run lint, vitest or prettier offline (see below), so please do
  once; `format` normalises any whitespace nit.
- Fonts are system stacks on purpose (CSP `font-src 'self'`, offline use). To
  self-host a face, add an `@font-face` and put its name first in `--font-sans`
  and `--font-mono`; nothing else changes.

## Verification

Done against a bundle of the real source, a mocked API and headless Chromium:

- Your **12 existing Playwright e2e specs, unmodified: 12/12 pass.**
- **Contrast audit** of every visible text node (real composited backgrounds,
  alpha and opacity included) across 17 UI states in both themes: no failures.
- **No horizontal overflow** at 320 / 360 / 390 / 768 / 1024 px, both themes.
- **24 behaviour checks**: theme persistence and OS tracking, no flash, drawer,
  search/filter, new-run flow, clipboard.
- `tsc --strict --noUnusedLocals` clean (React types shimmed offline).
- 38 logic tests (highlighter, pipeline model, formatting) executed.

Not run: ESLint, Prettier, and the vitest/jsdom component tests (no
`node_modules` offline). The new component tests follow your existing patterns.
