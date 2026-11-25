import type {
  DashboardReport,
  ExperimentConditionKind,
  QuantitativeResult,
} from "./types";

/** Explicit result-to-node associations take precedence over shared citations. */
export function resultNodeIds(
  report: DashboardReport,
  result: QuantitativeResult,
): string[] {
  const candidates =
    result.node_ids === undefined
      ? report.analysis.nodes
          .filter((node) =>
            result.evidence_ids.some((id) => node.evidence_ids.includes(id)),
          )
          .map((node) => node.id)
      : result.node_ids;
  return [
    ...new Set(
      candidates.filter((id) => report.analysis.nodes.some((node) => node.id === id)),
    ),
  ];
}

export const conditionLabels: Record<ExperimentConditionKind, string> = {
  dataset: "数据集",
  split: "数据划分",
  sample_size: "样本量",
  baseline: "比较基线",
  hyperparameters: "超参数",
  hardware: "硬件",
  evaluation_protocol: "评估协议",
  other: "其他条件",
};

/** Missing means not extracted in this report, never absent from the paper. */
export function missingResultConditions(
  result: QuantitativeResult,
): ExperimentConditionKind[] {
  if (result.missing_conditions !== undefined)
    return [...new Set(result.missing_conditions)];
  const present = new Set(
    (result.conditions || []).filter((c) => c.value.trim()).map((c) => c.kind),
  );
  const missing: ExperimentConditionKind[] = [];
  if (!result.dataset.trim() && !present.has("dataset")) missing.push("dataset");
  if (!result.condition.trim() && !present.has("evaluation_protocol"))
    missing.push("evaluation_protocol");
  return missing;
}
