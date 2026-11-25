/* TeX in prose: `$…$` and `$$…$$` as browser-native MathML.
 *
 * KaTeX (MIT, already shipped with the bundle) only parses; it is asked for
 * MathML output, so no KaTeX CSS or fonts are needed, and the result passes
 * through the same element-by-element whitelist as recognised formulas.
 * Delimiter handling follows EasyRead's markup.js (MIT).
 */
import katex from "katex";

const MATH_NS = "http://www.w3.org/1998/Math/MathML";
const MATH_TAGS = new Set(
  "math mi mn mo ms mtext mspace mrow mfrac msqrt mroot msub msup msubsup munder mover munderover mtable mtr mtd mstyle mpadded mphantom menclose semantics annotation mmultiscripts mprescripts none merror".split(
    " ",
  ),
);
const MATH_ATTRS = new Set(
  "mathvariant displaystyle scriptlevel fence separator stretchy symmetric largeop movablelimits accent accentunder lspace rspace linethickness columnalign rowalign columnspacing rowspacing notation display encoding width height depth".split(
    " ",
  ),
);
export function sanitizeMathML(source: string, inline = false): string | null {
  const doc = new DOMParser().parseFromString(source, "application/xml");
  if (doc.querySelector("parsererror")) return null;
  const clean = (el: Element): Element | null => {
    if (!MATH_TAGS.has(el.localName)) return null;
    const out = document.createElementNS(MATH_NS, el.localName);
    for (const attr of el.attributes)
      if (MATH_ATTRS.has(attr.localName) && !/[<>]|script:/i.test(attr.value))
        out.setAttribute(attr.localName, attr.value);
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) out.append(child.textContent || "");
      else if (child.nodeType === Node.ELEMENT_NODE) {
        const next = clean(child as Element);
        if (next) out.append(next);
      }
    }
    return out;
  };
  const root = doc.documentElement;
  if (root.localName !== "math") return null;
  const cleaned = clean(root);
  if (inline && cleaned) cleaned.setAttribute("display", "inline");
  return cleaned ? new XMLSerializer().serializeToString(cleaned) : null;
}

const cache = new Map<string, string | null>();
/** Formula fields already have math context. Remove only whole-expression wrappers. */
export function stripMathDelimiters(value: string): string {
  let text = value.trim();
  for (;;) {
    const fence = text.match(/^```(?:latex|tex|math)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/i);
    let next = fence ? fence[1].trim() : text;
    if (!fence) {
      for (const [open, close] of [
        ["$$", "$$"],
        ["\\[", "\\]"],
        ["\\(", "\\)"],
        ["$", "$"],
      ]) {
        if (
          text.length <= open.length + close.length ||
          !text.startsWith(open) ||
          !text.endsWith(close)
        )
          continue;
        const inner = text.slice(open.length, -close.length);
        const tokens: string[] = inner.match(/\\[\s\S]|[$\\]/g) || [];
        if (
          open === "$"
            ? tokens.includes("$") || tokens.at(-1) === "\\"
            : inner.includes(close)
        )
          continue;
        next = inner.trim();
        break;
      }
    }
    if (next === text) return text;
    text = next;
  }
}

/** Sanitised MathML for one TeX expression, or null when it does not parse. */
export function texToMathML(tex: string, display = false): string | null {
  tex = stripMathDelimiters(tex);
  const key = `${display ? "D" : "I"}${tex}`;
  if (cache.has(key)) return cache.get(key)!;
  let out: string | null = null;
  if (tex.trim() && tex.length <= 4000) {
    try {
      const html = katex.renderToString(tex, {
        output: "mathml",
        displayMode: display,
        throwOnError: true,
        strict: "ignore",
        trust: false,
        maxSize: 20,
        maxExpand: 300,
      });
      const math = html.match(/<math[\s\S]*<\/math>/)?.[0];
      out = math ? sanitizeMathML(math, !display) : null;
    } catch {
      out = null;
    }
  }
  if (cache.size > 800) cache.clear();
  cache.set(key, out);
  return out;
}

export type TexPiece = { text: string } | { tex: string; display: boolean };
/** Raw delimiter ranges, in original UTF-16 coordinates. */
export function texRanges(
  text: string,
): { start: number; end: number; tex: string; display: boolean }[] {
  const out = [];
  const pattern =
    /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$((?:\\\$|[^$\n])+?)(?<!\\)\$(?!\d)/g;
  for (const m of text.matchAll(pattern)) {
    if (m[4] !== undefined && (/^\s|\s$/.test(m[4]) || !m[4].trim())) continue;
    out.push({
      start: m.index,
      end: m.index + m[0].length,
      tex: (m[1] ?? m[2] ?? m[3] ?? m[4]).trim(),
      display: m[1] !== undefined || m[2] !== undefined,
    });
  }
  return out;
}
/** Split prose into text and TeX runs. Escaped `\$` stays a dollar sign. */
export function splitTex(text: string): TexPiece[] {
  const out: TexPiece[] = [];
  let last = 0;
  for (const range of texRanges(text)) {
    if (range.start > last) out.push({ text: text.slice(last, range.start) });
    out.push({ tex: range.tex, display: range.display });
    last = range.end;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out.map((p) => ("text" in p ? { text: p.text.replace(/\\\$/g, "$") } : p));
}
