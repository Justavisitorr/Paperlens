import { Fragment, type ReactNode } from "react";
import { texRanges, texToMathML } from "./tex";

/**
 * The small Markdown the reading assistant writes: headings, lists, quotes,
 * tables, code, bold, and [n] markers pointing at matched source quotes.
 * Parsed into data and rendered as React elements, so model text can never
 * become HTML.
 */
export type Inline =
  | { t: "text"; v: string }
  | { t: "strong"; v: string }
  | { t: "em"; v: string }
  | { t: "code"; v: string }
  | { t: "math"; v: string; display: boolean }
  | { t: "cite"; n: number[] };

export type MdBlock =
  | { t: "p"; lines: string[] }
  | { t: "h"; level: 1 | 2; v: string }
  | { t: "ul" | "ol"; items: string[] }
  | { t: "quote"; lines: string[] }
  | { t: "code"; v: string }
  | { t: "math"; v: string }
  | { t: "table"; head: string[]; rows: string[][] };

const LIST = /^\s*(?:[-*•]|\d+[.、)])\s+/;
const ORDERED = /^\s*\d+[.、)]\s+/;
const cells = (line: string) =>
  line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());

export function parseMarkdown(source: string): MdBlock[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const out: MdBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    // Display equations are one block even when the TeX spans several lines.
    const delimiter = /^(\$\$|\\\[)\s*$/.exec(line.trim());
    if (delimiter) {
      const close = delimiter[1] === "$$" ? "$$" : "\\]";
      const end = lines.findIndex(
        (candidate, at) => at > i && candidate.trim() === close,
      );
      if (end > i) {
        out.push({ t: "math", v: lines.slice(i + 1, end).join("\n") });
        i = end + 1;
        continue;
      }
    }
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
      i++;
      out.push({ t: "code", v: body.join("\n") });
      continue;
    }
    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      out.push({ t: "h", level: heading[1].length <= 2 ? 1 : 2, v: heading[2].trim() });
      i++;
      continue;
    }
    if (
      /^\s*\|.*\|\s*$/.test(line) &&
      /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1] || "")
    ) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]))
        rows.push(cells(lines[i++]));
      out.push({ t: "table", head, rows });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i]))
        body.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push({ t: "quote", lines: body });
      continue;
    }
    if (LIST.test(line)) {
      const ordered = ORDERED.test(line);
      const items: string[] = [];
      while (i < lines.length && lines[i].trim()) {
        if (LIST.test(lines[i]) && ORDERED.test(lines[i]) === ordered)
          items.push(lines[i].replace(LIST, ""));
        else if (!LIST.test(lines[i]) && items.length)
          // A wrapped line continues the item above.
          items[items.length - 1] += ` ${lines[i].trim()}`;
        else break;
        i++;
      }
      out.push({ t: ordered ? "ol" : "ul", items });
      continue;
    }
    const body: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(```|#{1,6}\s|>)/.test(lines[i]) &&
      !(body.length && /^(\$\$|\\\[)\s*$/.test(lines[i].trim())) &&
      !LIST.test(lines[i])
    )
      body.push(lines[i++]);
    out.push({ t: "p", lines: body });
  }
  return out;
}

const INLINE =
  /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[\d{1,2}(?:\s*[,，、]\s*\d{1,2})*\])|(\*[^*\s][^*\n]*\*)/g;

function parsePlainInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  const push = (piece: Inline) => {
    const prev = out[out.length - 1];
    if (piece.t === "cite" && prev?.t === "cite") prev.n.push(...piece.n);
    else if (piece.t === "text" && prev?.t === "text") prev.v += piece.v;
    else out.push(piece);
  };
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) push({ t: "text", v: text.slice(last, m.index) });
    const [all, code, strong, cite] = m;
    if (code) push({ t: "code", v: code.slice(1, -1) });
    else if (strong) push({ t: "strong", v: strong.slice(2, -2) });
    else if (cite)
      push({
        t: "cite",
        n: cite
          .slice(1, -1)
          .split(/[,，、]/)
          .map(Number),
      });
    else push({ t: "em", v: all.slice(1, -1) });
    last = m.index + all.length;
  }
  if (last < text.length) push({ t: "text", v: text.slice(last) });
  return out;
}

/** Code stays literal; TeX is isolated before Markdown emphasis or citation parsing. */
export function parseInline(text: string): Inline[] {
  const code = [...text.matchAll(/`[^`\n]+`/g)].map((m) => ({
    start: m.index,
    end: m.index + m[0].length,
  }));
  const emphasis = [...text.matchAll(INLINE)]
    .filter((m) => m[2] || (!m[1] && !m[3]))
    .map((m) => ({ start: m.index, end: m.index + m[0].length }));
  const math = texRanges(text).filter(
    (r) =>
      !code.some((c) => c.start < r.end && c.end > r.start) &&
      !emphasis.some((e) => e.start <= r.start && e.end >= r.end),
  );
  if (!math.length) return parsePlainInline(text);
  const out: Inline[] = [];
  let last = 0;
  for (const range of math) {
    out.push(...parsePlainInline(text.slice(last, range.start)));
    out.push({ t: "math", v: range.tex, display: range.display });
    last = range.end;
  }
  out.push(...parsePlainInline(text.slice(last)));
  return out;
}

function MathText({ tex, display }: { tex: string; display: boolean }) {
  const math = texToMathML(tex, display);
  return math ? (
    <span
      className={display ? "tex-display" : "tex-inline"}
      dangerouslySetInnerHTML={{ __html: math }}
    />
  ) : (
    <code className="tex-source">{tex}</code>
  );
}

/** Sentences like “[1][2]” merge; unknown numbers stay plain text. */
function Inlines({
  text,
  cite,
}: {
  text: string;
  cite?: (numbers: number[]) => ReactNode;
}) {
  return (
    <>
      {parseInline(text).map((piece, i) =>
        piece.t === "text" ? (
          <Fragment key={i}>{piece.v}</Fragment>
        ) : piece.t === "strong" ? (
          <strong key={i}>
            <Inlines text={piece.v} cite={cite} />
          </strong>
        ) : piece.t === "em" ? (
          <em key={i}>
            <Inlines text={piece.v} cite={cite} />
          </em>
        ) : piece.t === "code" ? (
          <code key={i}>{piece.v}</code>
        ) : piece.t === "math" ? (
          <MathText key={i} tex={piece.v} display={piece.display} />
        ) : (
          <Fragment key={i}>{cite ? cite(piece.n) : `[${piece.n.join(", ")}]`}</Fragment>
        ),
      )}
    </>
  );
}

export function Markdown({
  text,
  cite,
}: {
  text: string;
  cite?: (numbers: number[]) => ReactNode;
}) {
  return (
    <div className="md">
      {parseMarkdown(text).map((block, i) => {
        const inl = (v: string) => <Inlines text={v} cite={cite} />;
        switch (block.t) {
          case "h":
            return block.level === 1 ? (
              <h4 key={i}>{inl(block.v)}</h4>
            ) : (
              <h5 key={i}>{inl(block.v)}</h5>
            );
          case "ul":
          case "ol": {
            const List = block.t;
            return (
              <List key={i}>
                {block.items.map((item, j) => (
                  <li key={j}>{inl(item)}</li>
                ))}
              </List>
            );
          }
          case "quote":
            return <blockquote key={i}>{inl(block.lines.join(" "))}</blockquote>;
          case "code":
            return (
              <pre key={i}>
                <code>{block.v}</code>
              </pre>
            );
          case "math":
            return <MathText key={i} tex={block.v} display />;
          case "table":
            return (
              <div className="md-table" key={i}>
                <table>
                  <thead>
                    <tr>
                      {block.head.map((c, j) => (
                        <th key={j}>{inl(c)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, j) => (
                      <tr key={j}>
                        {row.map((c, k) => (
                          <td key={k}>{inl(c)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return (
              <p key={i}>
                {block.lines.map((line, j) => (
                  <Fragment key={j}>
                    {j > 0 && <br />}
                    {inl(line)}
                  </Fragment>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}
