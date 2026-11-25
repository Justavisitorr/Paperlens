import hljs from "highlight.js/lib/core";
import cpp from "highlight.js/lib/languages/cpp";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";

hljs.registerLanguage("cpp", cpp);
hljs.registerLanguage("python", python);
hljs.registerLanguage("javascript", javascript);

export interface CodeToken {
  text: string;
  classes: string;
}

/** Highlight the whole listing so strings/comments keep their state across pages.
 * Then split tokens at the original text boundaries for existing notes/selections.
 * Only Highlight.js's escaped text and span classes are read, never paper HTML.
 */
export function highlightCodeParts(parts: string[], language: string): CodeToken[][] {
  if (!hljs.getLanguage(language)) return parts.map((text) => [{ text, classes: "" }]);
  const html = hljs.highlight(parts.join("\n"), { language, ignoreIllegals: true }).value;
  const entities: Record<string, string> = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#x27;": "'",
  };
  const tokens: CodeToken[] = [];
  const scopes: string[] = [];
  for (const match of html.matchAll(/<span class="([^"]+)">|<\/span>|([^<]+)/g)) {
    if (match[1] !== undefined) scopes.push(match[1]);
    else if (match[0] === "</span>") scopes.pop();
    else
      tokens.push({
        text: match[2].replace(
          /&(?:amp|lt|gt|quot|#x27);/g,
          (entity) => entities[entity],
        ),
        classes: scopes.join(" "),
      });
  }
  let cursor = 0;
  const spans = tokens.map((token) => {
    const start = cursor;
    cursor += token.text.length;
    return { ...token, start, end: cursor };
  });
  let offset = 0;
  let at = 0;
  return parts.map((text) => {
    const end = offset + text.length;
    const result: CodeToken[] = [];
    while (at < spans.length && spans[at].end <= offset) at++;
    for (let i = at; i < spans.length && spans[i].start < end; i++) {
      const span = spans[i];
      result.push({
        text: span.text.slice(
          Math.max(0, offset - span.start),
          Math.min(span.text.length, end - span.start),
        ),
        classes: span.classes,
      });
    }
    offset = end + 1; // The page separator is rendered outside each note surface.
    return result;
  });
}
