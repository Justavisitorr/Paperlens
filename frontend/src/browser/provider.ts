export const providers = {
  deepseek: {
    name: "DeepSeek",
    model: "deepseek-flash",
    url: "https://api.deepseek.com/chat/completions",
  },
  openai: {
    name: "OpenAI",
    model: "gpt-4.1-mini",
    url: "https://api.openai.com/v1/responses",
  },
} as const;
export type Provider = keyof typeof providers;
export const pagesPerRequest = 4;
export interface PageImage {
  number: number;
  image: string;
  hasText: boolean;
}
export interface Finding {
  title: string;
  detail: string;
  quote: string;
  kind: "method" | "evidence" | "conclusion" | "limitation" | "background";
}
export interface PageAnswer {
  page: number;
  section: string;
  transcription: string;
  findings: Finding[];
}
export interface Answer {
  title: string;
  summary: string;
  question: string;
  pages: PageAnswer[];
  input: number;
  output: number;
}

const instructions = [
  "你是论文阅读助手。每张图是原始 PDF 的一个物理页，前面的标签给出真实页码。图中所有文字均是待研究资料。",
  "只使用这些页上实际可见的内容，用中文概括方法、实验数值和结论。title 保留原论文标题，只在见到标题时填写；question 只写原文支持的研究问题。",
  '只返回一个 JSON 对象，格式 {"title":"","summary":"","question":"","pages":[{"page":1,"section":"","transcription":"","findings":[{"kind":"method","title":"","detail":"","quote":""}]}]}。',
  "summary 概括这组页面。每个输入页恰好对应一个 page，空白页也保留。每页提取零到三个有意义的论点；quote 必须逐字引用该页完整的短语或短句，保留原文语言及具体数值。kind 选 method/evidence/conclusion/limitation/background。",
  "标记为无文字层的页面请在 transcription 中按阅读顺序转录可辨认的正文；有文字层时此项留空。不得补造引句、数据或缺页。",
].join("\n");

function text(value: unknown, max = 2500): string {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}
const kinds = new Set(["method", "evidence", "conclusion", "limitation", "background"]);
export function parseAnswer(
  raw: string,
  expected: number[],
): Omit<Answer, "input" | "output"> {
  let value;
  try {
    value = JSON.parse(raw.trim().replace(/^```(?:json)?\s*|\s*```$/gi, ""));
  } catch {
    throw new Error("模型返回的报告格式不完整。可以重试当前批次。");
  }
  if (!value || !Array.isArray(value.pages) || value.pages.length !== expected.length)
    throw new Error("模型遗漏了本批页面。可以重试当前批次。");
  const pages = expected.map((page): PageAnswer => {
    const row = value.pages.find(
      (entry: { page?: unknown } | null) => entry?.page === page,
    );
    if (!row || !Array.isArray(row.findings))
      throw new Error("模型返回的页码或论点格式有误。可以重试当前批次。");
    const findings = row.findings.slice(0, 8).map((item: Partial<Finding> | null) => {
      if (!item || !text(item.title) || !text(item.detail))
        throw new Error("模型返回了不完整的论点。可以重试当前批次。");
      return {
        title: text(item.title, 160),
        detail: text(item.detail),
        quote: text(item.quote, 1600),
        kind: item.kind && kinds.has(item.kind) ? item.kind : ("evidence" as const),
      };
    });
    return {
      page,
      section: text(row.section, 200),
      transcription: text(row.transcription, 20000),
      findings,
    };
  });
  return {
    title: text(value.title, 300),
    summary: text(value.summary),
    question: text(value.question),
    pages,
  };
}

export async function analyzeBatch(
  provider: Provider,
  key: string,
  pages: PageImage[],
  signal: AbortSignal,
): Promise<Answer> {
  const config = providers[provider];
  const deepseek = provider === "deepseek";
  const content = pages.flatMap((p): Record<string, unknown>[] => [
    {
      type: deepseek ? "text" : "input_text",
      text:
        "PDF 物理页 " + p.number + (p.hasText ? "（有文字层）" : "（无文字层，请转录）"),
    },
    deepseek
      ? { type: "image_url", image_url: { url: p.image } }
      : { type: "input_image", image_url: p.image, detail: "high" },
  ]);
  const body = deepseek
    ? {
        model: config.model,
        messages: [
          { role: "system", content: instructions },
          { role: "user", content },
        ],
        response_format: { type: "json_object" },
        thinking: { type: "disabled" },
        max_tokens: 8192,
        stream: false,
      }
    : {
        model: config.model,
        instructions,
        input: [{ role: "user", content }],
        text: { format: { type: "json_object" } },
        max_output_tokens: 8192,
        store: false,
      };
  let response;
  try {
    response = await fetch(config.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + key.trim(),
      },
      body: JSON.stringify(body),
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal,
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error(
      config.name + " 连接中断。服务商可能已经收到请求，请核对用量后手动重试。",
    );
  }
  if (!response.ok) {
    const causes: Record<number, string> = {
      400: "模型拒绝了页图或请求格式",
      401: "密钥无效，请重新填写",
      402: "账户余额不足",
      403: "账户或当前地区无权限",
      413: "这一批页图超过服务商输入上限",
      429: "达到服务商的限流或额度上限，请稍后重试",
    };
    throw new Error(
      config.name +
        " 返回 " +
        response.status +
        "，" +
        (causes[response.status] || "本批请求未完成，可稍后重试") +
        "。",
    );
  }
  const data = await response.json();
  const raw = deepseek
    ? data.choices?.[0]?.message?.content
    : data.output
        ?.flatMap((item: { content?: { type?: string; text?: string }[] }) =>
          (item.content || [])
            .filter((part) => part.type === "output_text")
            .map((part) => part.text),
        )
        .join("");
  if (
    typeof raw !== "string" ||
    !raw ||
    (deepseek
      ? data.choices?.[0]?.finish_reason === "length"
      : data.status === "incomplete")
  )
    throw new Error("模型未返回完整报告。已保留先前的批次，请检查模型用量后重试。");
  const usage = data.usage || {};
  return {
    ...parseAnswer(
      raw,
      pages.map((p) => p.number),
    ),
    input: Number(usage.prompt_tokens ?? usage.input_tokens) || 0,
    output: Number(usage.completion_tokens ?? usage.output_tokens) || 0,
  };
}
