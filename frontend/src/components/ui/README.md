# UI primitives

The shared component kit. Everything the console renders is built from these,
and every visual value they use comes from `src/styles/tokens.css` — no
component knows a colour, a size or a duration.

Storybook was ruled out (too heavy for eleven components); this file is the
substitute, and `src/components/ui/__tests__/` is the executable half of it.

## Conventions

One naming scheme across the whole kit:

| Prop | Meaning |
|---|---|
| `variant` | visual treatment: `default` \| `primary` \| `ghost` \| `danger` |
| `size` | scale: `sm` \| `md` |
| `tone` | status meaning: `good` \| `warn` \| `bad` \| `idle`, or `note` \| `error` |
| `loading` | work in flight; implies disabled and sets `aria-busy` |
| `disabled` | not actionable |
| `error` | a validation message, rendered and wired to the control |
| `hint` | supplementary context, wired via `aria-describedby` |
| `label` | always a visible label; never a placeholder |

## The kit

### `Button`
`variant`, `size`, `loading`, `loadingLabel`.
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
`tone: note | error`. `error` is `role="alert"` (assertive — a failure to react
to); `note` is `role="status"` (polite — advisory).

### `Card`, `EmptyState`
`Card` takes an optional `label` that renders a real `<section aria-label>`.
`EmptyState` takes an `action`, because an empty panel without a way to fill it
is a dead end.

### `Stat`, `StatGrid`
A number and its label. Always in a grid — a single tile reads as an orphan.

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