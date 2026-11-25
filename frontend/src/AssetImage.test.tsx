import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AssetImage } from "./AssetImage";

afterEach(() => vi.unstubAllGlobals());

it("keeps remote image requests out of the initial render and preserves its layout dimensions", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const html = renderToStaticMarkup(
    <AssetImage
      src={`/api/library/a/assets/${"a".repeat(64)}`}
      alt="Figure 2"
      width={1200}
      height={600}
    />,
  );
  expect(html).not.toContain("src=");
  expect(html).toContain('loading="lazy"');
  expect(html).toContain('width="1200" height="600"');
  expect(html).toContain('alt="Figure 2"');
  expect(fetch).not.toHaveBeenCalled();
});

it("leaves embedded offline image data directly readable without fetching", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const data = "data:image/png;base64,aW1hZ2U=";
  expect(renderToStaticMarkup(<AssetImage src={data} alt="Figure 2" />)).toContain(
    `src="${data}"`,
  );
  expect(fetch).not.toHaveBeenCalled();
});
