import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe } from 'jest-axe';

import { FollowUpPanel } from '../FollowUpPanel';
import type { RunDetail } from '../../types';

// The api module is the only seam the panel talks through.
const { addFollowup } = vi.hoisted(() => ({ addFollowup: vi.fn() }));
vi.mock('../../api', () => ({ api: { addFollowup } }));

const detail = (overrides: Partial<RunDetail> = {}): RunDetail =>
  ({
    run_id: 'run-1',
    requirement: 'A habit tracker',
    status: 'accepted',
    created_at: '2026-10-21T00:00:00Z',
    updated_at: '2026-10-21T00:00:00Z',
    tests_passed: 12,
    tests_failed: 0,
    attempts_dev: 0,
    attempts_test: 0,
    review_rounds: 0,
    wall_time: 42,
    followups: [],
    ...overrides,
  }) as RunDetail;

describe('FollowUpPanel', () => {
  beforeEach(() => {
    addFollowup.mockReset();
    addFollowup.mockResolvedValue({ run_id: 'run-3' });
  });

  it('says there is nothing to change yet when no change has been made', () => {
    render(<FollowUpPanel detail={detail()} onOpenRun={() => {}} />);
    expect(screen.getByText(/No changes yet/i)).toBeTruthy();
  });

  it('lists the changes that were applied', () => {
    render(
      <FollowUpPanel
        detail={detail({
          followups: [
            {
              run_id: 'run-2',
              prompt: 'add a skip button',
              status: 'accepted',
              created_at: '2026-10-21T00:10:00Z',
              error: null,
              tests_passed: 14,
              tests_failed: 0,
            },
          ],
        })}
        onOpenRun={() => {}}
      />
    );
    expect(screen.getByText('add a skip button')).toBeTruthy();
    expect(screen.getByText(/14 passed/)).toBeTruthy();
  });

  it('sends the instruction and opens the run it produced', async () => {
    const openRun = vi.fn();
    const user = userEvent.setup();
    render(<FollowUpPanel detail={detail()} onOpenRun={openRun} />);

    await user.type(screen.getByLabelText(/What should change/i), 'add a skip button');
    await user.click(screen.getByRole('button', { name: /Apply the change/i }));

    await waitFor(() => expect(openRun).toHaveBeenCalledWith('run-3'));
    expect(addFollowup).toHaveBeenCalledWith('run-1', 'add a skip button');
  });

  it('will not send an empty instruction', () => {
    render(<FollowUpPanel detail={detail()} onOpenRun={() => {}} />);
    const button = screen.getByRole('button', { name: /Apply the change/i });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<FollowUpPanel detail={detail()} onOpenRun={() => {}} />);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
