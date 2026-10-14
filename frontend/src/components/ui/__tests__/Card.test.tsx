import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Card, EmptyState } from '../Card';
import { Stat, StatGrid } from '../Stat';

describe('Card', () => {
  it('renders children in a card', () => {
    render(<Card>Content</Card>);
    expect(screen.getByText('Content')).toBeInTheDocument();
    expect(screen.getByText('Content').closest('.card')).toBeInTheDocument();
  });

  it('renders as section with aria-label when label provided', () => {
    render(<Card label="My Card">Content</Card>);
    expect(screen.getByRole('region', { name: 'My Card' })).toBeInTheDocument();
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<Card>Accessible</Card>);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});

describe('EmptyState', () => {
  it('renders message and action', () => {
    render(<EmptyState action={<button>Action</button>}>Nothing here</EmptyState>);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Action' })).toBeInTheDocument();
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<EmptyState>Accessible</EmptyState>);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});

describe('Stat', () => {
  it('renders value and label', () => {
    render(<Stat value={42} label="Tests passed" />);
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('Tests passed')).toBeInTheDocument();
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<Stat value="—" label="Accessible" />);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});

describe('StatGrid', () => {
  it('renders children in a grid', () => {
    render(
      <StatGrid>
        <Stat value={1} label="One" />
        <Stat value={2} label="Two" />
      </StatGrid>
    );
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('One')).toBeInTheDocument();
    expect(screen.getByText('Two')).toBeInTheDocument();
  });
});
