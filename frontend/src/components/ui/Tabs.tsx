import { useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';

import { cn } from '../../lib/cn';

export interface TabItem<T extends string> {
  id: T;
  label: string;
  /** A count, appended to the label — e.g. review comments, generated UI files. */
  badge?: ReactNode;
}

export interface TabsProps<T extends string> {
  items: ReadonlyArray<TabItem<T>>;
  value: T;
  onChange: (_id: T) => void;
  /** The active tab's content. The tab list decides what is active, so the
   *  caller keeps rendering logic in one place. */
  children: ReactNode;
  label: string;
}

/**
 * The run-detail tab strip.
 *
 * This is a real `tablist`: one tab stop for the whole strip (roving
 * tabindex), arrow keys to move between tabs, Home/End to jump to the ends.
 * Activation is automatic — arrowing to a tab shows it — because every panel
 * here is already in memory and rendering one costs nothing.
 *
 * Ten tabs will not fit a phone, so the strip scrolls horizontally. It is a
 * scroll container with `role="tablist"`, which is fine: the tab buttons
 * inside still move focus as the browser scrolls them into view.
 */
export function Tabs<T extends string>({ items, value, onChange, children, label }: TabsProps<T>) {
  const listRef = useRef<HTMLDivElement>(null);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const last = items.length - 1;
    const here = items.findIndex(t => t.id === value);
    if (here < 0) return;

    let next: number;
    switch (e.key) {
      case 'ArrowRight':
        next = here === last ? 0 : here + 1;
        break;
      case 'ArrowLeft':
        next = here === 0 ? last : here - 1;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = last;
        break;
      default:
        return;
    }
    e.preventDefault();
    onChange(items[next].id);
    // Move focus with the selection, so the next arrow press starts from here.
    listRef.current
      ?.querySelector<HTMLButtonElement>(`#tab-${CSS.escape(items[next].id)}`)
      ?.focus();
  };

  return (
    <>
      <div className="tabs" role="tablist" aria-label={label} ref={listRef} onKeyDown={onKeyDown}>
        {items.map(item => {
          const selected = item.id === value;
          return (
            <button
              key={item.id}
              id={`tab-${item.id}`}
              role="tab"
              type="button"
              aria-selected={selected}
              aria-controls={`tabpanel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              className={cn(selected && 'active')}
              onClick={() => onChange(item.id)}
            >
              {item.label}
              {item.badge}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`tabpanel-${value}`}
        aria-labelledby={`tab-${value}`}
        // Focusable so a keyboard user can scroll a tall panel after arrowing
        // into it, and so the panel is reachable when it holds no controls.
        tabIndex={0}
      >
        {children}
      </div>
    </>
  );
}
