import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Badge } from '../Badge';

describe('Badge', () => {
  it('renders with tone and children', () => {
    render(<Badge tone="good">Success</Badge>);
    const badge = screen.getByText('Success');
    expect(badge).toHaveClass('badge');
    expect(badge).toHaveClass('good');
  });

  it('applies all tone variants', () => {
    render(<Badge tone="warn">Warning</Badge>);
    expect(screen.getByText('Warning')).toHaveClass('warn');

    render(<Badge tone="bad">Error</Badge>);
    expect(screen.getByText('Error')).toHaveClass('bad');

    render(<Badge tone="idle">Idle</Badge>);
    expect(screen.getByText('Idle')).toHaveClass('idle');
  });

  it('applies pulse animation when requested', () => {
    render(
      <Badge tone="good" pulse>
        Processing
      </Badge>
    );
    const dot = screen.getByText('Processing').parentElement?.querySelector('.dot');
    expect(dot).toHaveClass('pulse');
  });

  it('renders decorative dot', () => {
    render(<Badge tone="good">Status</Badge>);
    const dot = screen.getByText('Status').parentElement?.querySelector('.dot');
    expect(dot).toBeInTheDocument();
    expect(dot).toHaveAttribute('aria-hidden', 'true');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<Badge tone="good">Accessible</Badge>);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
