import { Fragment } from "react";
import { splitTex, texToMathML, type TexPiece } from "./tex";

// Report prose occasionally omits delimiters. Only unmistakable TeX scripts or
// the kernel notation are inferred; source text and stored offsets stay intact.
export function analysisPieces(text: string): TexPiece[] {
  return splitTex(text).flatMap((piece) => {
    if (!("text" in piece)) return [piece];
    const pattern =
      /(?<![\w\\])(?:ker\s+[A-Za-z](?:[_^]\{[^{}\n]{1,100}\})*(?:\s*=\s*[A-Za-z](?:[_^]\{[^{}\n]{1,100}\})*)?|(?:\\[A-Za-z]+|[A-Za-z])(?:[_^](?:\{[^{}\n]{1,100}\}|[A-Za-z0-9]))+)/g;
    const parts: TexPiece[] = [];
    let at = 0;
    for (const match of piece.text.matchAll(pattern)) {
      if (match.index > at) parts.push({ text: piece.text.slice(at, match.index) });
      parts.push({ tex: match[0].replace(/^ker\s+/, "\\ker "), display: false });
      at = match.index + match[0].length;
    }
    if (at < piece.text.length) parts.push({ text: piece.text.slice(at) });
    return parts;
  });
}

export function AnalysisText({ text }: { text: string }) {
  return (
    <>
      {analysisPieces(text).map((piece, index) => {
        if ("text" in piece) return <Fragment key={index}>{piece.text}</Fragment>;
        const math = texToMathML(piece.tex, false);
        return math ? (
          <span
            key={index}
            className="tex-inline"
            dangerouslySetInnerHTML={{ __html: math }}
          />
        ) : (
          <Fragment key={index}>{piece.tex}</Fragment>
        );
      })}
    </>
  );
}

/**
 * Text the reader typed (questions, quotes): only explicit `$…$`, `$$…$$`,
 * `\(…\)` and `\[…\]` become math; everything else stays as written.
 */
export function TexText({ text }: { text: string }) {
  return (
    <>
      {splitTex(text).map((piece, index) => {
        if ("text" in piece) return <Fragment key={index}>{piece.text}</Fragment>;
        const math = texToMathML(piece.tex, piece.display);
        return math ? (
          <span
            key={index}
            className={piece.display ? "tex-display" : "tex-inline"}
            dangerouslySetInnerHTML={{ __html: math }}
          />
        ) : (
          <code key={index} className="tex-source">
            {piece.tex}
          </code>
        );
      })}
    </>
  );
}

/** SVG labels keep native text so SVG/PNG export and keyboard focus still work.
 * Common inline scripts use the same KaTeX MathML tree as the detail view. */
export function analysisSvgFragment(text: string): DocumentFragment {
  const ns = "http://www.w3.org/2000/svg";
  const fragment = document.createDocumentFragment();
  const span = () => document.createElementNS(ns, "tspan");
  const render = (element: Element): Node => {
    const out = document.createDocumentFragment();
    const children = [...element.children];
    const tag = element.localName;
    if (tag === "annotation") return out;
    if (tag === "mspace") {
      out.append(document.createTextNode("\u2009"));
      return out;
    }
    if (["msub", "msup", "msubsup"].includes(tag)) {
      out.append(render(children[0]));
      children.slice(1).forEach((child, index) => {
        const script = span();
        script.setAttribute(
          "baseline-shift",
          tag === "msup" || index === 1 ? "super" : "sub",
        );
        script.setAttribute("font-size", "70%");
        script.append(render(child));
        out.append(script);
      });
    } else if (!children.length)
      out.append(document.createTextNode(element.textContent || ""));
    else children.forEach((child) => out.append(render(child)));
    return out;
  };
  for (const piece of analysisPieces(text)) {
    if ("text" in piece) {
      fragment.append(document.createTextNode(piece.text));
      continue;
    }
    const source = texToMathML(piece.tex, false);
    const math = source
      ? new DOMParser().parseFromString(source, "application/xml").documentElement
      : null;
    const supported =
      math &&
      [...math.querySelectorAll("*")].every((el) =>
        [
          "semantics",
          "annotation",
          "mrow",
          "mi",
          "mn",
          "mo",
          "mtext",
          "mspace",
          "msub",
          "msup",
          "msubsup",
        ].includes(el.localName),
      );
    if (supported) {
      const group = span();
      group.setAttribute("class", "graph-math");
      group.setAttribute("aria-label", piece.tex);
      group.setAttribute("font-family", "Cambria Math, STIX Two Math, serif");
      group.append(render(math));
      fragment.append(group);
    } else fragment.append(document.createTextNode(piece.tex));
  }
  return fragment;
}
