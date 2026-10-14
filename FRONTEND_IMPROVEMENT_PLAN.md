# Improvement Plan — Frontend Design System, Accessibility, and Responsive Layout

> Status: **all seven steps implemented and verified** — lint, typecheck, 40
> unit tests with jest-axe, 12 Playwright end-to-end tests, and a green build.
> Decisions locked: dark theme only, structured so a light theme can be
> appended · mobile **and** desktop are both mandatory targets · steps 1–6 of
> the nine-phase brief · Storybook and Lighthouse CI are out, replaced by
> `components/README.md` · tests ship alongside the components they cover.

Supersedes nothing. Touches only `frontend/` plus one line-group of
`frontend/nginx/default.conf`. No backend change, no API contract change, no
layout redesign.

## 0. Progress

| Step | Status |
|---|---|
| 1. Design tokens | **done** — `src/styles/tokens.css`; 39 contrast pairs re-measured, 0 failing |
| 2. Component consistency | **done** — 11 primitives in `src/components/ui/` |
| 3. Accessibility | **done** — live regions, real tablist, focus trap, nested-interactive removed |
| 4. Responsive | **done** — topbar wrap, `.scroll-x`, sidebar drawer, `clamp()` |
| 5. UX states | **done** — skeletons, retry, inline validation, toasts, confirm on delete |
| 6. Tests and lint | **done** — Vitest + jest-axe + Playwright + ESLint + Prettier + CI jobs |
| 7. nginx security headers | **done** — verified by serving the config and reading the headers back |

### 0.1 Two bugs found while verifying, not by reading

Both were introduced by the refactor and caught only because the end-to-end
suite drives a real browser:

1. **The console unmounted to a blank page when the Playground tab was
   selected.** Moving the tab bodies into `renderTab()` dropped the
   `<Suspense>` wrappers that had surrounded the lazy panels, so selecting a
   tab made React throw *"A component suspended while responding to synchronous
   input"* and the whole tree unmounted. Fixed by restoring a boundary per
   panel — and by adding an `ErrorBoundary` (PERF-05, §1.6) so a render error
   can never again blank the page with no way back.
2. **The confirm dialog stole focus on every parent render.** Its focus effect
   depended on an inline `onCancel`, so it re-ran on every re-render, yanking
   focus back to the confirm button. Fixed by holding the callback in a ref
   updated from an effect.

### 0.2 Measurements

| | Before | After |
|---|---|---|
| Contrast pairs failing WCAG 2.2 AA | 7 | **0** of 39 re-measured |
| Hard-coded colours in `styles.css` | 25 | **0** |
| Token names referenced but undefined | — | **0** |
| Component library | 0 | **11 primitives** |
| Tests | 0 | **40 unit + 12 e2e** |
| Security headers on the SPA document | **0** | **4** (verified over HTTP) |
| Frontend CI jobs | 1 (build) | **3** (lint+unit, e2e, images) |

## 0. Background: where things stand

The console at `frontend/` is a single-page operator UI for the pipeline:
submit a requirement, watch five agents work, read the artefacts, optionally
boot the generated app in a live playground. 19 tracked files, 2,667 lines of
TS/TSX and 740 of CSS, React 18 + Vite + TypeScript, no UI library, no state
library, no CSS framework, no router.

It works. The defect density, however, is concentrated in exactly the places
this plan touches, and three of the findings are bugs rather than polish:

- the login **password field renders with light UA styling** in a dark app,
- the topbar and the 8-column evaluation table **overflow horizontally** on
  narrow viewports,
- the SPA document and every hashed asset are **served with no security
  headers** at all.

Two measured facts shape the phasing:

| Fact | Value | Consequence |
|---|---|---|
| Contrast pairs failing WCAG 2.2 AA | 5 text, 2 non-text | Phase 1 is mandatory before anything else — it is a correctness fix, not polish |
| Interactive elements with no accessible name or name via `title` only | 6 | Phase 2 extracts components so the fix lands once |
| Bundle | 175.0 KB raw / **55.4 KB gzip**, one chunk | Small enough that no bundle work is urgent; splitting is optional |
| Fonts / images | 0 | Nothing to do for LCP-image, font-subsetting or CLS. LCP *is* the JS parse. |

`tsc --noEmit` passes today and the build is green. The bar is "no
regression", not "make it build".

## 1. Verified findings

Measured on this machine against the working tree, not inferred. Contrast
ratios are computed sRGB luminances; file references are current line numbers.

### 1.1 Contrast: seven pairs fail, and `--text-faint` is load-bearing

`--text-faint #64748b` is the single worst offender because it is not used for
anything decorative. It carries `.hint`, `.empty`, **every** field label, the
topbar subtitle and every run-list timestamp:

| Pair | Ratio | Need | Verdict |
|---|---|---|---|
| `--text #e5eaf2` on `--bg` | 15.89 | 4.5 | pass |
| `--text-dim #94a3b8` on `--bg-raised` | 6.92 | 4.5 | pass |
| **`--text-faint #64748b` on `--bg`** | **4.03** | 4.5 | **fail** |
| **`--text-faint` on `--bg-raised`** | **3.73** | 4.5 | **fail** |
| **`--accent #6366f1` as text on `--bg`** | **4.30** | 4.5 | **fail** |
| **`--accent` as text on `--bg-raised`** | **3.97** | 4.5 | **fail** |
| **`#fff` on `--accent` (primary button)** | **4.47** | 4.5 | **fail by 0.03** |
| badge `.good` / `.warn` / `.bad` | 8.4 – 10.1 | 4.5 | pass |
| `.alert.error` / `.alert.note` | 8.6 / 11.0 | 4.5 | pass |
| `td.pass` / `td.fail` / `.sev.*` | 5.7 – 11.6 | 4.5 | pass |
| `--dot --good` / `--warn` / `--bad` | 4.7 – 8.3 | 3.0 | pass |
| **`--border-strong #2d3a4d` on `--bg-raised`** | **1.54** | 3.0 | **fail** — input and button borders |
| **`--border #1f2937` on `--bg-raised`** | **1.21** | 3.0 | **fail** — but see below |

`--accent` is **fine** for non-text use: focus ring 4.30, active border 3.82,
timeline accents 4.06–7.57. Only its use as small text fails, so the fix is a
separate text-role token, not a rebrand.

Card borders are the exception that proves the rule. `--bg-raised` against
`--bg` is 1.08 and `--bg-inset` against `--bg-raised` is 1.06: a card is
separated by **fill**, not by its 1px edge. Raising the border to 3:1 would
make it the only thing distinguishing a card from the page and would look
wrong. Card borders stay decorative; **control** borders go to 3:1.

### 1.2 Three bugs

| Finding | Location | Why it is a bug, not a nit |
|---|---|---|
| Password field unstyled | `styles.css:84` | The selector is `input[type="text"], textarea`. The login field is `type="password"` (`LoginPage.tsx:48`), so it renders with the UA default light styling inside a dark app. No `color-scheme` declaration either, so scrollbars and other controls are light too. |
| No security headers on the SPA | `nginx/default.conf:88,95` | `location /assets/` and `location /` each declare their own `add_header`, and nginx **replaces** the inherited server-level set rather than adding to it. So `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and `COOP` are dropped exactly where they matter most. The file already documents this gotcha at line 57 for `/preview/`. |
| Horizontal overflow | `styles.css:110`, `EvalPanel.tsx:38` | The topbar is `display:flex` with `white-space: nowrap` and no wrap, holding h1 + 2 subtitles + 2 badges + button + version. The evaluation table has 8 columns in a `.card` with no overflow container. Both scroll the page sideways. |

### 1.3 Accessibility

| ID | Sev | Finding | Location |
|---|---|---|---|
| A11Y-02 | High | `role="button" tabIndex={0}` **div containing a real `<button>`** — axe `nested-interactive`. No `aria-current` on the selected run either. | `RunList.tsx:35` |
| A11Y-03 | High | 10 tabs are plain buttons: no `role="tablist"`/`tab`/`tabpanel`, no `aria-selected`, no arrow-key navigation, panels unlabelled. | `App.tsx:487` |
| A11Y-04 | High | No live regions at all. Status changes and SSE pipeline events are silent to screen readers; `.alert` has no `role="alert"`. | `App.tsx` |
| A11Y-05 | High | `ConfirmModal` has zero focus management: no initial focus, no trap (Tab reaches the page behind), no restore, `aria-label` instead of `aria-labelledby` on a visible `<h2>`. | `ConfirmModal.tsx:22` |
| A11Y-07 | Medium | No `:focus-visible` anywhere. Inputs use `outline: none` + a 1px border swap; buttons keep only the UA ring. | `styles.css:96` |
| A11Y-08 | Medium | Sticky topbar (`z-index: 20`) can obscure keyboard-focused content — no `scroll-padding-top`. WCAG 2.2 Focus Not Obscured (2.4.11). | `styles.css:110` |
| A11Y-09 | Medium | Icon-only buttons (↻, ✕) use `title` as their only accessible name. | `RunList.tsx:27,56` |
| A11Y-10 | Medium | Infinite `.pulse` on the status dot with no `prefers-reduced-motion` guard. | `styles.css:291` |
| A11Y-11 | Medium | Login: error not linked via `aria-describedby`, no `aria-invalid`, first heading is `<h2>` with no `<h1>`/`<main>`, no password reveal. | `LoginPage.tsx` |
| A11Y-12 | Low | No skip link; run list is not a landmark; the 4 ablation checkboxes are not in a `fieldset`/`legend`; "ablate" is unexplained jargon. | — |

### 1.4 Visual consistency

| ID | Sev | Finding |
|---|---|---|
| VIS-01 | High | `.card > h2, .card > h3` uppercases **every** heading to 12px faint. "New run", "No run selected", "Evaluation" and the modal title all render at the same size as a table header — there is no hierarchy between a page's headings and its content. `styles.css:315` |
| VIS-02 | High | **25 hard-coded colour literals** outside `:root`: `#1a2333`, `#4f52e0`, `#161b2e`, `#b3261e`/`#8f1d17`, `#6ee7b7`, `#fcd34d`, `#fca5a5`, `#c7d2fe`, `#fff`, a `#2a2f3a` fallback inside an inline style, plus 14 `rgba()` values. |
| VIS-03 | Medium | 8 font sizes (10, 11, 12, 12.5, 13, 14, 15, 20px), no scale. `font-weight: 650` is a variable-font value that non-variable system stacks round unpredictably per platform. |
| VIS-04 | Medium | 5 radii (4, 5, 6, 8, 999px) and 14 arbitrary spacing values (2,4,5,6,7,8,9,10,12,14,16,18,20,60). |
| VIS-05 | Medium | `.modal` reuses `.card`, so the confirm dialog inherits the 12px eyebrow title treatment. |
| VIS-06 | Medium | 4 ad-hoc button variants, 2 padding sizes, no size axis, no shared API. |
| VIS-07 | Low | Semantic colours are unnamed pairs — `--good` for dots/borders vs `#6ee7b7` for text — so nothing in the token file links them. |

### 1.5 Responsive

| ID | Sev | Finding |
|---|---|---|
| RWD-01 | High | Topbar has no wrap strategy; overflows below ~600px. |
| RWD-02 | High | `EvalPanel`'s 8-column table has no overflow container; `UiPanel`'s 3-column table is borderline. |
| RWD-03 | Medium | At ≤900px the layout collapses to one column, pushing the whole run list above the content; `.sidebar` keeps `max-height: calc(100vh - 53px)` — a magic number coupled to the topbar's height. |
| RWD-04 | Medium | No fluid type or spacing (`clamp()`), no container queries. |
| RWD-05 | Low | `pre.code { max-height: 62vh }` is a fixed viewport value. |

### 1.6 Performance

Bundle is 175.0 KB raw / 55.4 KB gzip in **one chunk**, no fonts, no images,
no render-blocking assets. That is small; there is no urgent performance work.
Two real inefficiencies, both logic rather than bundle:

| ID | Sev | Finding |
|---|---|---|
| PERF-01 | Medium | **Double fetch per poll** — `tick()` calls `loadDetail(selected)` and then `api.getRun(selected)` again for the same run. Two GETs every 1.5s per active run. `App.tsx:158` |
| PERF-02 | Medium | `PlaygroundPanel` polls `/playground` every 1.5s **indefinitely**, including in the settled `stopped` state. `PlaygroundPanel.tsx:50` |
| PERF-03 | Medium | `liveEvents` grows unbounded and `events` re-sorts the whole array each render; the memo never hits because `detail` is a new object every poll. |
| PERF-05 | Medium | No error boundary — a render throw leaves a blank page. |
| PERF-04/06 | Low | No code splitting (`chunkSizeWarningLimit: 900` masks it); no favicon, so `/favicon.ico` falls through the SPA fallback and returns HTML. |

### 1.7 Code quality

| ID | Sev | Finding |
|---|---|---|
| CQ-01 | High | `App.tsx` is 677 lines: 17 `useState`s, 6 async handlers, 10 conditional tab renders. Tab state is not in the URL, so no deep links and browser Back does nothing. |
| CQ-02 | Medium | ~20 inline `style={{}}` objects carrying magic values, across 7 files. |
| CQ-03 | Medium | Duplication: `shortTime` written twice with different formats (`RunList.tsx:12`, `PipelineTimeline.tsx:62`); the `.stat` block in `App` **and** `EvalPanel`; badge markup in 3 files with a duplicated tone map; `.alert` markup in 5. |
| CQ-04 | Medium | Silent `catch { /* transient */ }` in `refreshRuns`/`loadDetail`/`refreshHealth` — a failing API leaves stale data with no signal. |
| CQ-05 | Medium | `types.ts` mixes wire types with presentation maps; `statusTone` is UI logic in a types module. |
| CQ-06 | Medium | `PipelineEvent` carries `[key: string]: unknown`, so every payload field is untyped and `describe()` casts to `Record<string, unknown>` eight times. |
| CQ-08 | Medium | The playground iframe has no `sandbox` attribute; a generated app is same-origin with the console and can read parent DOM and the session cookie. |
| CQ-09 | Medium | No ESLint, no Prettier. CI runs `typecheck` and `build` and nothing else. |
| CQ-10 | Low | No `noUncheckedIndexedAccess`; `detail.artifacts` typed but never rendered; `csrfToken()` exported but only used inside `api.ts`. |

### 1.8 What is already good — do not regress it

Correct CSRF double-submit with `credentials: "same-origin"`. SSE dedup by
`seq` with a polling fallback. `statusTone` never communicates status by colour
alone — every badge and table verdict carries text. `lang="en"`, viewport and
meta description present. nginx sets CSP and `SAMEORIGIN` on `/preview/`.
Hashed assets get `immutable`. `.dot` is decorative and empty. `tsconfig` is
strict with `noUnusedLocals`/`noUnusedParameters`.

## 2. Phase 1 — Design tokens

Create `frontend/src/styles/tokens.css` as the single source of truth, using
W3C Design Tokens Community Group naming (`2025.10` where practical). Import
it before `styles.css` in `main.tsx`. `styles.css` keeps structure and layout
rules and consumes tokens only.

### 2.1 The five contrast fixes

Four colour changes and one weight change. Everything else is a like-for-like
rename. **Only one is visible to a user at a glance**: the primary button.

| Token | Now | After | Requirement satisfied |
|---|---|---|---|
| `--text-faint` | `#64748b` (3.73) | **`#78899f`** — 4.96 card / 5.37 page | 4.5:1 for `.hint`, `.empty`, all labels |
| `--accent-text` *(new)* | — | **`#818cf8`** — 5.94 / 6.28 | 4.5:1 for `.story .id`, active file tab |
| `--accent-solid` *(new)* | `#6366f1` (4.47) | **`#5457d6`** — white on it 5.66 | primary button label |
| `--border-control` *(new)* | `#2d3a4d` (1.54) | **`#5a677a`** — 3.09 / 3.26 | SC 1.4.11, control boundaries |
| `--font-weight-semibold` | `650` | **`600`** | consistent across platforms |

`--accent #6366f1` **stays** for non-text use — rings, active borders,
timeline accents — where it already passes 3:1. Primary hover is `#5b5ee0`:
lighter than the base, matching the existing direction, and still 5.09:1 with
white text.

### 2.2 Token groups

```
--color-bg-canvas / -surface / -inset          #0b0f16 #121826 #0d121c
--color-text-primary / -secondary / -tertiary  #e5eaf2 #94a3b8 #78899f
--color-border-default / -control              #1f2937 #5a677a
--color-accent / -solid / -text / -hover       #6366f1 #5457d6 #818cf8 #5b5ee0
--color-status-success / -warning / -danger    #10b981 #f59e0b #ef4444
  …each with a -text variant carrying the lighter tint used on tinted fills
--color-danger-solid / -hover                  #b3261e #8f1d17
--font-sans / --font-mono
--font-size-2xs…2xl (11,12,13,14,16,20,24) + --font-weight-regular…bold
--space-1…12 (4px base)   --radius-sm/md/lg/pill   --shadow-1/2
--z-base / -sticky / -modal / -toast
--duration-fast / -base   --ease-standard / -out
--focus-ring-width / -offset / -color
@media (prefers-reduced-motion: reduce)  → --duration-*: 1ms
@media (prefers-color-scheme: light)    → reserved slot, documented, empty
```

Every token carries a comment naming the WCAG threshold it satisfies, so the
next person does not have to re-measure the file. The light block is
deliberately empty: dark-only today, but appending a
`[data-theme="light"]` override later must not require touching a component.

### 2.3 New global rules

`:focus-visible { outline: 2px solid var(--focus-ring-color); outline-offset: 2px }`
(9.6:1, clears 2.4.11). `html { scroll-padding-top: 60px }` for the sticky
topbar. The input selector gains `input[type="password"]`, fixing §1.2.
`:root { color-scheme: dark }`. A `prefers-reduced-motion` guard on `.pulse`.

## 3. Phase 2 — Component consistency

There is no component library today: one bare `<button>` with 4 CSS classes,
one input selector, one card, one badge, one alert, one modal, each bespoke
per feature. Phase 2 **extracts** them.

`src/components/ui/`: `Button`, `Input`, `Textarea`, `Checkbox`, `Card`,
`Badge`, `Alert`, `Modal`, `Tabs`, `Stat`, `EmptyState`.

One naming convention across all of them: `variant` for visual treatment,
`size` for scale, `tone` for status meaning, `loading` and `disabled` for
state. Each supports default, hover, focus-visible, active, disabled, loading
and error. Existing call sites are adapted **without changing the DOM class
names**, so `styles.css` keeps working and the diff stays reviewable.

`Modal` gains the focus behaviour it is missing outright: initial focus,
containment, restore on close, `aria-labelledby` on the visible heading.
`Tabs` gains real `tablist`/`tab`/`tabpanel` semantics and arrow-key
navigation.

## 4. Phase 3 — Accessibility (WCAG 2.2 AA)

Semantic landmarks and heading order; `role="alert"` on alerts; `aria-live`
on the status badge and the timeline region; `aria-current` plus removal of
the nested-interactive pattern in `RunList`; `aria-describedby` and
`aria-invalid` on the login fields; `fieldset`/`legend` around the ablation
checkboxes; `<h1>` and `<main>` on the login route. Contrast, focus
visibility, target size and reduced motion are handled in §2.

Explicitly **not** changed: keyboard operability of the run list (it already
handles Enter and Space), and the absence of colour-only status (it is already
correct).

## 5. Phase 4 — Responsive design

Mobile-first, with desktop as a first-class target — both are mandatory.

Topbar wrap and collapse strategy. A `.scroll-x` wrapper for both tables so
they never scroll the page. The sidebar becomes collapsible at ≤900px, which
also removes the `calc(100vh - 53px)` coupling to the topbar. `clamp()` on
`.stat .v` and card padding. Container queries where a component must adapt to
its container rather than the viewport.

Verified at **320, 375, 768, 1024, 1280 and 1536**.

## 6. Phase 5 — UX states and feedback

Skeletons for the run list and the evaluation table. Retry affordances on the
currently silent `catch {}` paths. Inline validation on the requirement form
with the entered text preserved on error. Toasts on run start/stop/restart/
delete. **Delete gets a `ConfirmModal`** — it is currently instant and
unrecoverable. Empty states get a clear next action rather than a bare
"No runs yet."

## 7. Phase 6 — Tests, lint, and the security headers

**Vitest + Testing Library + `jest-axe`** per shared component, landing in the
same diff as the component itself. **ESLint** (flat config, with
`jsx-a11y` on — it catches A11Y-02/03/05 immediately — plus import ordering)
and **Prettier** with pre-commit hooks and a CI lint job. **Playwright** for
three flows.

### 7.1 What Playwright runs against

`cli.py --stub` **cannot** back Playwright: it calls `graph.workflow.run_stub()`,
prints JSON and exits without binding a port (`cli.py:31`). Three backend facts
change the approach:

1. `AGENT_TEAM_TESTING=1` does **not** stub generation. It only short-circuits
   the Ollama reachability probe (`agents/llm.py:231`) and blanks the model
   list in `/health` (`routers/health.py:45`).
2. `blocked` is a **first-class terminal state** with its own UI paths —
   `statusTone` → `bad` badge, the `detail.error` hint, an `error` timeline
   node. It is a valid assertion target, so generation need not succeed.
3. Auth is drivable from the environment: `AUTH_PASSWORD` + `AUTH_SECRET`
   hash on load (`app/auth.py:148`), so a fixture server authenticates as
   `admin` with a known plaintext.

So, with **no backend changes**:

- **A — real server, no LLM.** `uvicorn` with `AUTH_PASSWORD`, `AUTH_SECRET`,
  `SANDBOX_MODE=subprocess`, plus the Vite dev server via Playwright's
  `webServer`. Covers login → submit → poll → settle → read the `blocked`
  verdict → restart-with-confirm → delete.
- **B — a seeded `accepted` run.** A checked-in fixture directory in the shape
  `backend/tests/conftest.py`'s `fake_pipeline` writes, with `RUNS_DIR` pointed
  at it. This renders the whole run-detail console — stories, plan, OpenAPI,
  code, tests, report, review, UI — which no E2E can otherwise reach without a
  live model. Effectively free fixture data.
- **Playground: start only, no boot.** A booted preview spawns a real uvicorn
  subprocess executing generated code on the CI runner. Assert
  start → graceful failure → error alert; skip the booted iframe.

### 7.2 nginx

Restate `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and
`COOP` inside `location /` and `location /assets/`, or hoist them so the
per-location `add_header` cannot drop them. This changes the production image,
so it ships as its own commit, reviewable on its own.

## 8. Out of scope — left open in §1

Excluded by the scope decision, deliberately, because each would re-touch
`App.tsx` after steps 1–5 have already done so:

- **CQ-01** the `App.tsx` split and tab → URL state (no deep links, Back does
  nothing)
- **PERF-01 / PERF-02** the double fetch per poll and the idle playground poll
- **PERF-03** unbounded `liveEvents` and the memo that never hits
- **PERF-04 / PERF-05** `React.lazy` and an error boundary
- **CQ-06** `PipelineEvent` as a discriminated union
- **CQ-08** `sandbox` on the iframe — adding it may break previews that assume
  same-origin, so it needs a manual test against a real preview
- **PERF-07** SSR/prerender; LCP is the JS parse and 55 KB gzip is acceptable

## 9. Definition of done

- No visual or behavioural regression in existing features
- Zero critical axe violations; WCAG 2.2 AA checklist passed
- No hard-coded colours, font sizes or spacing outside the token file
- Every shared component has all states, is documented in
  `components/README.md`, and is covered by a unit + `jest-axe` test
- Lint, type-check and tests pass in CI
- No horizontal page scroll at 320px through 1536px

Because Lighthouse CI is out of scope, the Core Web Vitals targets have **no
automated gate** — only the bundle numbers in §0. `@lhci/cli` is a single dev
dependency if that trade is worth revisiting.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Four colours change; screenshots differ | Exactly five visible places: hints/labels, story IDs, active file tab, primary buttons, input borders. Every badge, alert and table verdict must stay pixel-identical. |
| `--border-control #5a677a` is noticeably lighter than today | The price of SC 1.4.11 on inputs. `#556276` (2.87:1) is available if the lighter look is preferred over strict compliance. |
| Component extraction is the largest diff | DOM class names are preserved, so `styles.css` keeps working and each component lands with its own test |
| nginx header fix changes the production image | Its own commit; the `/preview/` CSP must be re-verified at the same time |
| Mobile and desktop are both mandatory, which is more Phase 4 than estimated | Budget one day; verify all six widths before calling step 4 done |

## 11. Order of work

1. Tokens — `styles/tokens.css`, `styles.css`, `main.tsx`, `index.html`
2. Component consistency — `src/components/ui/*` plus call sites
3. Semantics — `RunList`, `LoginPage`, `RequirementForm`, `App` live regions
4. Responsive — `styles.css`, `App`, `RunList`, `EvalPanel`, `UiPanel`
5. UX states — skeletons, retry, validation, toasts, confirm on delete
6. Tests and lint — per-component tests, ESLint/Prettier, Playwright
7. nginx security headers

Step 2 depends on 1. Step 3 depends on 2. Step 4 is independent after 1.
Step 6 depends on 2. Step 7 is independent.