# UI primitives

The shared component kit. Everything the console renders is built from these,
and every visual value they use comes from `src/styles/tokens.css` — no
component knows a colour, a size or a duration.

Storybook was ruled out (too heavy for eleven components); this file is the
substitute, and `src/components/ui/__tests__/` is the executable half of it.

## Conventions

One naming scheme across the whole kit:

| Prop       | Meaning                                                                                                      |
| ---------- | ------------------------------------------------------------------------------------------------------------ |
| `variant`  | visual treatment: `default` \| `primary` \| `ghost` \| `danger`                                              |
| `size`     | scale: `sm` \| `md`                                                                                          |
| `tone`     | status meaning: `good` \| `warn` \| `bad` \| `idle`, or `note` \| `error` \| `success` \| `warning` (alerts) |
| `loading`  | work in flight; implies disabled and sets `aria-busy`                                                        |
| `disabled` | not actionable                                                                                               |
| `error`    | a validation message, rendered and wired to the control                                                      |
| `hint`     | supplementary context, wired via `aria-describedby`                                                          |
| `label`    | always a visible label; never a placeholder                                                                  |

## The kit

### `Button`

`variant`, `size`, `loading`, `loadingLabel`. Add `className="icon-btn"` for a
square icon-only button — and give it an `aria-label`, because an icon is never
its accessible name.
States: default, hover, focus-visible, active, disabled, loading.
`loading` implies `disabled` and swaps in `loadingLabel`, because a button that
still says "Restart" while restarting reports nothing.

### `Field` — `Input`, `Textarea`, `Checkbox`

Every control wires its own `id`, visible `label`, and any `error`/`hint`
together, so no call site can get the pairing wrong. `useId()` supplies the id.
`Input` additionally takes `invalid` (mark without a message) and `describedBy`
(point at a form-level error), which is how the sign-in page shows one message
rather than two.

`required` renders a `*` that is `aria-hidden` — the attribute on the input is
what a screen reader reads.

### `Badge`

`tone`, `pulse`, `title`. The dot is decorative and `aria-hidden`; the status
**word** is what carries the meaning, so status is never colour-only.

### `Alert`

`tone: note | error | success | warning`. `error` is `role="alert"` (assertive —
a failure to react to); the others are `role="status"` (polite). Each carries an
icon that repeats the tone for people who cannot tell the colours apart; the icon
is decorative to assistive tech.

### `Card`, `EmptyState`

`Card` takes an optional `label` that renders a real `<section aria-label>`.
`EmptyState` takes an `action`, because an empty panel without a way to fill it
is a dead end.

### `Stat`, `StatGrid`

A number and its label, with an optional `tone` (`good` | `bad`) that colours the
value when it is a verdict. The label still says which, so colour only reinforces.
Always in a grid — a single tile reads as an orphan.

### `Modal`

Focus moves in on open, Tab is contained, focus is restored to the trigger on
close, Escape and backdrop both cancel, and `busy` blocks dismissal so a
destructive action cannot be fired twice. `aria-labelledby` points at the
visible heading.

Callbacks are held in a ref updated from an effect, so the focus effect depends
only on `open`. Without that, an inline `onCancel` re-runs the effect on every
parent render and steals focus mid-interaction.

The two `jsx-a11y` rules covering the backdrop and the dialog are disabled for
this file: the backdrop is a click target that is deliberately not a control,
and the keyboard equivalent (Escape plus the focus trap) is handled by the
effect.

### `Tabs`

A real `tablist` / `tab` / `tabpanel`. One tab stop for the strip (roving
tabindex), Arrow keys to move, Home/End to jump. Activation is automatic —
every panel here is already in memory. The panel is `tabindex=0` so a keyboard
user can scroll a tall one.

`items` may carry a `group`: tabs with the same group sit together and a thin
divider marks where it changes. It is purely visual — the strip is still one
tablist. `CountBadge` renders a pill whose parentheses are visually hidden, so
the tab still reads "Review (2)" to assistive tech.

Callers pass `items` (with optional per-tab `badge`) and render the active
panel as `children`. Tab ids are used in DOM ids, so a tab id must be a valid
CSS identifier.

### `Skeleton`, `SkeletonGroup`

Shimmering placeholders, `aria-hidden` with the group owning `aria-busy` so a
screen reader hears "busy" once instead of a pile of empty boxes. The shimmer
stops under `prefers-reduced-motion`.

### `Toast`, `ToastList`

`role="alert"` for errors, `role="status"` otherwise. Errors get twice the
timeout because they carry a reason. The list is capped at four; the oldest
falls off.

### `ErrorBoundary`

Contains a render error to the subtree that caused it and offers a retry.
Without it any throw unmounts the whole console — a blank page that also loses
the selected run and everything typed into the requirement box.

## Rules

- No component may hard-code a colour, font size, spacing value or duration.
  If a value is needed, it belongs in `src/styles/tokens.css` first.
- A component that renders text must give it an accessible name.
- Every interactive element must be reachable and operable by keyboard, and
  show a visible focus indicator — `:focus-visible` is global.
- New primitives need a test in `__tests__/` with a `jest-axe` assertion.

### `Icon`, `BrandMark`

The icon set, drawn inline as 24px outline glyphs (the CSP allows no icon font or
remote image). Every icon is `aria-hidden`; a control gets its name from text or
`aria-label`. `loader` spins, and stops under `prefers-reduced-motion`.

### `CodeBlock`

Read-only code or log output: a toolbar (name or language, line count, **Copy**),
optional line numbers and light syntax colouring from `lib/highlight.ts`.
Exactly one `<pre class="code">` holds the text, so it stays selectable and
searchable; highlighting never changes the text (a test pins this). Line numbers
are CSS counters, so they are never selected or copied. `preProps` lets a caller
make the `<pre>` a `tabpanel`, as `FileViewer` does.

### `ThemeSwitch`

System / Light / Dark as a group of `aria-pressed` buttons — a control that
changes the screen should show every choice and which one is current. State
lives in `lib/theme.ts`, a tiny external store, so every switch stays in step.

## Theming

Two themes ship, `dark` (the default block on `:root`) and `light`
(`:root[data-theme='light']`), both in `styles/tokens.css`. To add a third,
add one more block that redefines the colour tokens; nothing else changes.
Every foreground/background pair in that file is annotated with its measured
contrast ratio, and must keep clearing WCAG AA when edited.
