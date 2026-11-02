export function fmtTime(v: string | null | undefined): string {
  if (!v) return "–";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString();
}

export function fmtRelative(v: string | null | undefined): string {
  if (!v) return "never";
  const s = (Date.now() - new Date(v).getTime()) / 1000;
  if (s < 0) return `in ${fmtDuration(-s)}`;
  return `${fmtDuration(s)} ago`;
}

export function fmtDuration(s: number): string {
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
