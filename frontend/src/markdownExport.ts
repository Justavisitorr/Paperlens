/** Keep every source line inside its Markdown container, including blank lines. */
const linesOf = (text: string) => text.split(/\r\n|\r|\n/);

export function markdownQuote(text: string): string {
  return linesOf(text)
    .map((line) => `> ${line}`)
    .join("\n");
}

export function markdownListItem(text: string, marker = "-"): string {
  const [first, ...rest] = linesOf(text);
  const indent = " ".repeat(marker.length + 1);
  return [`${marker} ${first}`, ...rest.map((line) => `${indent}${line}`)].join("\n");
}
