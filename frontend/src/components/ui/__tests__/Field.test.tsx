import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Input } from '../Field';

describe('Input', () => {
  it('renders label and input', () => {
    render(<Input label="Username" placeholder="Enter name" />);
    expect(screen.getByLabelText('Username')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter name')).toBeInTheDocument();
  });

  it('shows error message and sets aria-invalid', () => {
    render(<Input label="Email" error="Invalid email" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid email');
    expect(screen.getByLabelText('Email')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Email')).toHaveAttribute('aria-describedby');
  });

  it('shows hint when no error', () => {
    render(<Input label="Name" hint="Your full name" />);
    expect(screen.getByText('Your full name')).toBeInTheDocument();
  });

  it('marks required', () => {
    render(<Input label="Required field" required />);
    const input = screen.getByRole('textbox', { name: /required field/i });
    expect(input).toBeRequired();
  });

  it('forwards invalid and describedBy for form-level errors', () => {
    render(<Input label="Password" type="password" invalid describedBy="form-error" />);
    expect(screen.getByLabelText('Password')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Password')).toHaveAttribute('aria-describedby', 'form-error');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<Input label="Test" />);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
