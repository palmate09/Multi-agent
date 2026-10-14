/** Join class names, dropping anything falsy. Keeps conditional class logic
 *  out of the components without pulling in a dependency. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
