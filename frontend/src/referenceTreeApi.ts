export interface ReferenceTreeNode {
  id: string;
  title: string;
  year: number | null;
  url: string;
  reference_count: number | null;
}

export interface ReferenceTreeBranch {
  state:
    | "available"
    | "unavailable"
    | "unresolved"
    | "not_found"
    | "ambiguous"
    | "rate_limited"
    | "disabled"
    | "error";
  message: string;
  node: ReferenceTreeNode | null;
  references: ReferenceTreeNode[];
  offset: number;
  next_offset: number | null;
  total: number | null;
  cached: boolean;
  checked_at: string;
  source: string;
  source_url: string;
}

/** Only same-origin account APIs receive a paper ID; public providers see no PDF. */
export async function loadReferenceBranch(
  paperId: string,
  referenceId: string,
  options: {
    node?: string;
    offset?: number;
    refresh?: boolean;
    signal?: AbortSignal;
  } = {},
): Promise<ReferenceTreeBranch> {
  if (location.protocol === "file:") throw new Error("请在连接后端的网站中查看引用树。");
  const params = new URLSearchParams();
  if (options.node) params.set("node", options.node);
  if (options.offset) params.set("offset", String(options.offset));
  if (options.refresh) params.set("refresh", "true");
  const response = await fetch(
    `/api/library/${encodeURIComponent(paperId)}/references/${encodeURIComponent(referenceId)}/tree?${params}`,
    {
      signal: options.signal,
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    },
  );
  if (!response.ok) {
    if (response.status === 401 || response.status === 403)
      throw new Error("请重新登录后查看引用树。");
    if (response.status === 404)
      throw new Error(
        "暂时找不到这条引用，或电脑端尚未加载引用树功能。请刷新或更新后端。",
      );
    throw new Error("引用树暂时无法加载，请检查后端连接后重试。");
  }
  return (await response.json()) as ReferenceTreeBranch;
}

export function mergeReferenceBranches(
  previous: ReferenceTreeBranch | undefined,
  next: ReferenceTreeBranch,
): ReferenceTreeBranch {
  if (!previous || !next.offset || next.state !== "available") return next;
  const references = new Map(previous.references.map((node) => [node.id, node]));
  next.references.forEach((node) => references.set(node.id, node));
  return { ...next, references: [...references.values()] };
}

export function referenceBranchStatus(
  id: string,
  ancestors: string[],
): "cycle" | "limit" | "expandable" {
  if (ancestors.includes(id)) return "cycle";
  return ancestors.length >= 6 ? "limit" : "expandable";
}
