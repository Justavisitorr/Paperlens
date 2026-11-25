import type { ProcessingProfileId } from "./api";

/**
 * Mirror of the server rule for a value still being edited: economy halves and
 * rounds down, detailed doubles, both kept inside the field's own range.
 * The server's numbers stay authoritative once saved.
 */
export function tierPreview(
  value: number,
  { min = 0, max = Number.MAX_SAFE_INTEGER }: { min?: number; max?: number },
): Record<ProcessingProfileId, number> {
  const scale = (factor: number) =>
    value === 0
      ? 0
      : Math.max(
          Math.min(Math.max(1, min), value),
          Math.min(max, Math.floor(value * factor)),
        );
  return { economy: scale(0.5), standard: value, detailed: scale(2) };
}
