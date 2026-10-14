import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { axe } from 'jest-axe';
import { Modal } from '../Modal';

describe('Modal', () => {
  it('renders when open', () => {
    render(
      <Modal open title="Confirm" confirmLabel="Yes" onConfirm={vi.fn()} onCancel={vi.fn()}>
        <p>Are you sure?</p>
      </Modal>
    );
    expect(screen.getByRole('dialog', { name: 'Confirm' })).toBeInTheDocument();
    expect(screen.getByText('Are you sure?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Yes' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('does not render when closed', () => {
    render(
      <Modal open={false} title="Confirm" confirmLabel="Yes" onConfirm={vi.fn()} onCancel={vi.fn()}>
        <p>Are you sure?</p>
      </Modal>
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('calls onConfirm when confirm button clicked', () => {
    const onConfirm = vi.fn();
    render(
      <Modal open title="Confirm" confirmLabel="Yes" onConfirm={onConfirm} onCancel={vi.fn()}>
        <p>Are you sure?</p>
      </Modal>
    );
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('calls onCancel when cancel button clicked', () => {
    const onCancel = vi.fn();
    render(
      <Modal open title="Confirm" confirmLabel="Yes" onConfirm={vi.fn()} onCancel={onCancel}>
        <p>Are you sure?</p>
      </Modal>
    );
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('shows loading state on confirm button', () => {
    render(
      <Modal
        open
        title="Confirm"
        confirmLabel="Yes"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        busy
        loadingLabel="Working..."
      >
        <p>Working...</p>
      </Modal>
    );
    const buttons = screen.getAllByRole('button');
    const confirmBtn = buttons.find(
      b => b.textContent?.includes('Working') || b.textContent?.includes('Yes')
    );
    expect(confirmBtn).toBeInTheDocument();
    expect(confirmBtn).toBeDisabled();
    expect(confirmBtn).toHaveAttribute('aria-busy', 'true');
    expect(confirmBtn).toHaveTextContent('Working…');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(
      <Modal open title="Accessible" confirmLabel="OK" onConfirm={vi.fn()} onCancel={vi.fn()}>
        <p>Test</p>
      </Modal>
    );
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
