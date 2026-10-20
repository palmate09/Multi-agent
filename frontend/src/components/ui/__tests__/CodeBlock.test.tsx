import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { axe } from 'jest-axe';
import { CodeBlock } from '../CodeBlock';

const SOURCE = 'def hello():\n    return "hi"  # greet\n';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CodeBlock', () => {
  it('keeps the source as plain text in a single pre.code', () => {
    const { container } = render(<CodeBlock code={SOURCE} language="python" />);
    const pres = container.querySelectorAll('pre.code');
    expect(pres).toHaveLength(1);
    // Highlighting must never change what a person reads or searches for.
    expect(pres[0]).toHaveTextContent('def hello():');
    expect(pres[0]).toHaveTextContent('return "hi" # greet');
  });

  it('colours tokens without altering the text', () => {
    const { container } = render(<CodeBlock code={SOURCE} language="python" />);
    expect(container.querySelector('.tok-keyword')).toHaveTextContent('def');
    expect(container.querySelector('.tok-string')).toHaveTextContent('"hi"');
    expect(container.querySelector('.tok-comment')).toHaveTextContent('# greet');
  });

  it('reports the line count and omits line numbers on request', () => {
    const { container, rerender } = render(<CodeBlock code={SOURCE} language="python" />);
    expect(screen.getByText(/2 lines/)).toBeInTheDocument();
    expect(container.querySelector('pre.code')).toHaveClass('numbered');
    rerender(<CodeBlock code={SOURCE} language="python" lineNumbers={false} />);
    expect(container.querySelector('pre.code')).not.toHaveClass('numbered');
  });

  it('forwards panel semantics to the pre so a caller can make it a tabpanel', () => {
    render(
      <CodeBlock
        code="x"
        preProps={{ id: 'file-panel', role: 'tabpanel', 'aria-labelledby': 't', tabIndex: 0 }}
      />
    );
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'file-panel');
  });

  it('copies the exact source (no line numbers) and confirms', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<CodeBlock code={SOURCE} language="python" />);

    fireEvent.click(screen.getByRole('button', { name: /copy/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SOURCE));
    expect(await screen.findByRole('button', { name: /copied/i })).toBeInTheDocument();
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<CodeBlock code={SOURCE} language="python" fileName="app.py" />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
