import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Alert } from '../Alert';

describe('Alert', () => {
  it('renders error tone with role=alert', () => {
    render(<Alert tone="error">Something went wrong</Alert>);
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
    expect(screen.getByRole('alert')).toHaveClass('error');
  });

  it('renders note tone with role=status', () => {
    render(<Alert tone="note">Advisory information</Alert>);
    expect(screen.getByRole('status')).toHaveTextContent('Advisory information');
    expect(screen.getByRole('status')).toHaveClass('note');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<Alert tone="error">Accessible</Alert>);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
