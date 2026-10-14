import { useId } from 'react';
import type { InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from 'react';

import { cn } from '../../lib/cn';

interface BaseFieldProps {
  /** Visible label text. Always rendered — there is no placeholder-as-label. */
  label: ReactNode;
  /** Validation message. Rendered in a live region and wired to the control. */
  error?: string | null;
  /** Extra context under the control, wired in with `aria-describedby`. */
  hint?: ReactNode;
  required?: boolean;
  children?: ReactNode;
}

function FieldShell({
  label,
  error,
  hint,
  required,
  htmlFor,
  describedBy,
  children,
}: BaseFieldProps & { htmlFor: string; describedBy?: string }) {
  return (
    <div className={cn('field', error && 'field-invalid')}>
      <label htmlFor={htmlFor}>
        {label}
        {required && (
          <span className="req" aria-hidden="true">
            *
          </span>
        )}
      </label>
      {children}
      {hint && !error && (
        <div className="field-hint" id={describedBy}>
          {hint}
        </div>
      )}
      {error && (
        // role="alert" so a validation failure is announced the moment it
        // appears, not only when the user next reaches the field.
        <div className="field-error" id={describedBy} role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

export interface InputProps
  extends BaseFieldProps, Omit<InputHTMLAttributes<HTMLInputElement>, 'aria-describedby'> {
  id?: string;
  /** Marks the control invalid without rendering a message. For when the
   *  message belongs to the form as a whole — e.g. "invalid username or
   *  password", which is neither field's fault specifically. */
  invalid?: boolean;
  /** Points at an existing element, usually a form-level error. */
  describedBy?: string;
}

/** Labelled text input. The id, the label and the error are wired together
 *  here so no call site can get that pairing wrong. */
export function Input({
  label,
  error,
  hint,
  required,
  id,
  invalid,
  describedBy,
  className,
  ...rest
}: InputProps) {
  const auto = useId();
  const inputId = id ?? auto;
  const own = hint || error ? `${inputId}-desc` : undefined;
  // Both a field-level message and a form-level one can be described.
  const desc = [own, describedBy].filter(Boolean).join(' ') || undefined;

  return (
    <FieldShell
      label={label}
      error={error}
      hint={hint}
      required={required}
      htmlFor={inputId}
      describedBy={own}
    >
      <input
        id={inputId}
        className={cn(className)}
        aria-invalid={error || invalid ? true : undefined}
        aria-describedby={desc}
        required={required}
        {...rest}
      />
    </FieldShell>
  );
}

export interface TextareaProps
  extends BaseFieldProps, Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'aria-describedby'> {
  id?: string;
}

export function Textarea({ label, error, hint, required, id, className, ...rest }: TextareaProps) {
  const auto = useId();
  const areaId = id ?? auto;
  const descId = `${areaId}-desc`;

  return (
    <FieldShell
      label={label}
      error={error}
      hint={hint}
      required={required}
      htmlFor={areaId}
      describedBy={hint || error ? descId : undefined}
    >
      <textarea
        id={areaId}
        className={cn(className)}
        aria-invalid={error ? true : undefined}
        aria-describedby={hint || error ? descId : undefined}
        required={required}
        {...rest}
      />
    </FieldShell>
  );
}

export interface CheckboxProps extends Omit<
  InputHTMLAttributes<HTMLInputElement>,
  'type' | 'aria-describedby'
> {
  label: ReactNode;
  /** Extra context, wired in with `aria-describedby`. */
  hint?: ReactNode;
  /** Renders the checkbox inside a `fieldset` alongside its siblings. */
  name?: string;
}

/** A checkbox and its label, always in that order in the DOM so the click
 *  target is the text as well as the box. */
export function Checkbox({ label, hint, name, className, ...rest }: CheckboxProps) {
  const auto = useId();
  const boxId = rest.id ?? auto;
  const descId = `${boxId}-desc`;

  return (
    <label className={cn('check', className)} htmlFor={boxId}>
      <input
        id={boxId}
        type="checkbox"
        name={name}
        aria-describedby={hint ? descId : undefined}
        {...rest}
      />
      <span>{label}</span>
      {hint && (
        <span className="field-hint" id={descId}>
          {hint}
        </span>
      )}
    </label>
  );
}
