import { describe, expect, it } from "vitest";
import { missingResultConditions, resultNodeIds } from "./experimentConditions";
import type { QuantitativeResult } from "./types";
import { reviewReport } from "./readingReview.fixtures";

describe("experiment conditions", () => {
  it("prefers explicit result node associations and only falls back for old reports", () => {
    const report = reviewReport(),
      result = report.quantitative_results![0];
    expect(resultNodeIds(report, result)).toEqual(["n2"]);
    expect(resultNodeIds(report, { ...result, node_ids: [] })).toEqual([]);
    expect(resultNodeIds(report, { ...result, node_ids: undefined })).toEqual([
      "n2",
      "n3",
    ]);
  });
  it("uses explicit missing conditions and remains compatible with older reports", () => {
    const result = reviewReport().quantitative_results![0];
    expect(missingResultConditions(result)).toEqual(["sample_size", "hardware"]);
    expect(
      missingResultConditions({
        ...result,
        conditions: undefined,
        missing_conditions: undefined,
        dataset: "",
      }),
    ).toEqual(["dataset", "evaluation_protocol"]);
    expect(
      missingResultConditions({
        ...result,
        missing_conditions: [],
      } as QuantitativeResult),
    ).toEqual([]);
  });
});
