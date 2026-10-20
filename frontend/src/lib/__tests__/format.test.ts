import { describe, it, expect } from 'vitest';
import { formatDuration } from '../format';

describe('formatDuration', () => {
  it.each([
    [0.2, '<1s'],
    [12, '12s'],
    [59.6, '1m 00s'],
    [65, '1m 05s'],
    [3725, '62m 05s'],
  ])('formats %s seconds as %s', (seconds, expected) => {
    expect(formatDuration(seconds)).toBe(expected);
  });

  it('shows a dash for values that are not durations', () => {
    expect(formatDuration(-1)).toBe('—');
    expect(formatDuration(Number.NaN)).toBe('—');
  });
});
