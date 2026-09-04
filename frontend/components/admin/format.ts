export function formatUtc(value: string | null): string {
  if (value === null) return "Never";
  return `${new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(value))} UTC`;
}

export function statusLabel(value: string): string {
  return value.toLowerCase().replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
