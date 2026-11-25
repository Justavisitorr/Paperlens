import {
  getProcessingProfiles,
  type ProcessingProfileId,
  type ProcessingProfiles,
} from "./api";

export function unlimitedBudgetWarning(
  profile: ProcessingProfiles["profiles"][number],
): string {
  const names = profile.unlimited_limits || [];
  return names.length
    ? `${names.join("、")}设为 0（不限）。处理可能耗时更长，并产生更多模型费用。总模型请求上限仍为 ${profile.limits.max_requests} 次，单次识别和文件资源保护仍生效。可暂停或取消，已发出的请求仍可能计费。确认按此预算开始处理？`
    : "";
}

/** Fetch current settings before either upload or source import starts. */
export async function confirmProcessingBudget(
  id: ProcessingProfileId,
): Promise<boolean | null> {
  const catalog = await getProcessingProfiles();
  const profile = catalog.profiles.find((p) => p.id === id);
  if (!profile) throw new Error("未能读取当前处理预算，请稍后重试。");
  const warning = unlimitedBudgetWarning(profile);
  return warning ? (window.confirm(warning) ? true : null) : false;
}
