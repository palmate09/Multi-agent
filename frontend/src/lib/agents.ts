/**
 * The agents a run goes through, in order, with what each one does. Shown on
 * the sign-in page and on the empty home screen, so a first-time visitor can
 * tell what the product does before they have run anything.
 *
 * Kept next to (not inside) the components so both screens say the same thing.
 */
export interface AgentStep {
  name: string;
  does: string;
}

export const AGENT_STEPS: ReadonlyArray<AgentStep> = [
  { name: 'PM', does: 'Turns your requirement into user stories with acceptance criteria.' },
  { name: 'Reasoner', does: 'Plans the approach and flags risks and edge cases. Advisory only.' },
  { name: 'Designer', does: 'Writes the OpenAPI contract everything else is built against.' },
  {
    name: 'Developer',
    does: 'Implements the API to match the contract, and fixes it when tests fail.',
  },
  { name: 'Tester', does: 'Writes tests from the contract and runs them in an isolated sandbox.' },
  { name: 'Reviewer', does: 'Checks the code with static analysis and leaves review comments.' },
];
