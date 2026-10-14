import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Tabs } from '../Tabs';

const TABS = [
  { id: 'a', label: 'Tab A' },
  { id: 'b', label: 'Tab B' },
  { id: 'c', label: 'Tab C' },
] as const;

describe('Tabs', () => {
  it('renders tabs with correct roles', () => {
    render(
      <Tabs items={TABS} value="a" onChange={vi.fn()}>
        <div>Panel A</div>
      </Tabs>
    );

    expect(screen.getByRole('tablist')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tab A' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tab B' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tab C' })).toBeInTheDocument();
    expect(screen.getByRole('tabpanel')).toBeInTheDocument();
  });

  it('marks active tab with aria-selected', () => {
    render(
      <Tabs items={TABS} value="b" onChange={vi.fn()}>
        <div>Panel B</div>
      </Tabs>
    );

    expect(screen.getByRole('tab', { name: 'Tab B' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Tab A' })).toHaveAttribute('aria-selected', 'false');
  });

  it('calls onChange when tab clicked', () => {
    const onChange = vi.fn();
    render(
      <Tabs items={TABS} value="a" onChange={onChange}>
        <div>Panel</div>
      </Tabs>
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Tab C' }));
    expect(onChange).toHaveBeenCalledWith('c');
  });

  it('renders badge on tab', () => {
    const items = [
      { id: 'a', label: 'Tab A', badge: '3' },
      { id: 'b', label: 'Tab B' },
      { id: 'c', label: 'Tab C' },
    ] as const;
    render(
      <Tabs items={items} value="a" onChange={vi.fn()}>
        <div>Panel</div>
      </Tabs>
    );
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(3);
    expect(tabs[0]).toHaveTextContent('3');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(
      <Tabs items={TABS} value="a" onChange={vi.fn()}>
        <div>Panel</div>
      </Tabs>
    );
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
