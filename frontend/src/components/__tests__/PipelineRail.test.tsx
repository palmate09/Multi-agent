import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { axe } from 'jest-axe';
import { PipelineRail } from '../PipelineRail';
import type { PipelineEvent } from '../../types';

const events = [
  { seq: 1, node: 'pipeline', phase: 'start', ts: '2026-10-17T08:10:00Z', nodes: ['pm', 'designer', 'developer'] },
  { seq: 2, node: 'pm', phase: 'done', ts: '2026-10-17T08:10:05Z', stories: 3 },
  { seq: 3, node: 'designer', phase: 'start', ts: '2026-10-17T08:10:06Z' },
] as PipelineEvent[];

describe('PipelineRail', () => {
  it('renders nothing for a run with no events', () => {
    const { container } = render(<PipelineRail events={[]} running={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says each stage state in words, not only colour', () => {
    render(<PipelineRail events={events} running />);
    const list = screen.getByRole('list', { name: 'Pipeline stages' });
    expect(list).toHaveTextContent('PM: done');
    expect(list).toHaveTextContent('Designer: running');
    expect(list).toHaveTextContent('Developer: waiting');
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<PipelineRail events={events} running />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
