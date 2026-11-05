// Credits in plain words (docs/design/credits.md).

export function creditsText(x: number): string {
  const rounded = Math.round(x * 100) / 100;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(2);
}

const KINDS: Record<string, string> = {
  start: "Start credits",
  grant: "Free credits from the coordinator",
  request: "Your request, approved",
  give: "Given",
  receive: "Received",
  spend: "Your job ran on their computer",
  earn: "Your computer ran their job",
  pool_retry: "Your computer ran a retry pool paid for",
  move_out: "Moved to another group",
  move_in: "Moved from another group",
  standby: "Your computer waited for work",
};

export function creditKindText(kind: string): string {
  return KINDS[kind] ?? kind;
}

/** The cost line on a job page: before it runs, and what it cost. */
export function jobCostText(c: { expected?: number | null; most?: number; spent?: number } | null | undefined): string | null {
  if (!c) return null;
  const parts: string[] = [];
  if (c.expected != null) parts.push(`about ${creditsText(c.expected)} credits`);
  if (c.most != null) parts.push(`at most ${creditsText(c.most)}`);
  const plan = parts.join(", ");
  return c.spent ? `${plan}; cost so far: ${creditsText(c.spent)}` : plan;
}
