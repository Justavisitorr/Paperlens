export type ProcessingSpeedMode = "normal" | "fast" | "ultra";

export const SPEED_LABELS: Record<ProcessingSpeedMode, string> = {
  normal: "普通",
  fast: "加速",
  ultra: "极速",
};

/** Old callers can keep sending the acceleration flag. Explicit modes take priority. */
export const normalizeSpeedMode = (
  value: ProcessingSpeedMode | boolean,
): ProcessingSpeedMode =>
  typeof value === "boolean" ? (value ? "fast" : "normal") : value;

export const nextSpeedMode = (mode: ProcessingSpeedMode): ProcessingSpeedMode =>
  mode === "normal" ? "fast" : mode === "fast" ? "ultra" : "normal";
