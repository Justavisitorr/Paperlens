import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ImageViewer } from "./ImageViewer";
import { imageBounds, imageFit, imagePan, imageZoom } from "./imageViewerGeometry";

describe("image display geometry", () => {
  it("fits the rotated extent of wide and tall images without cutting their axes", () => {
    const source = { width: 1200, height: 300 },
      viewport = { width: 390, height: 600 };
    expect(imageBounds(source, 90)).toEqual({ width: 300, height: 1200 });
    expect(imageBounds(source, 270)).toEqual({ width: 300, height: 1200 });
    expect(imageBounds(source, 180)).toEqual(source);
    for (const rotation of [0, 90, 180, 270]) {
      const size = imageBounds(source, rotation),
        scale = imageFit(source, viewport, rotation);
      expect(size.width * scale).toBeLessThanOrEqual(viewport.width - 32);
      expect(size.height * scale).toBeLessThanOrEqual(viewport.height - 32);
      expect(imagePan({ x: 999, y: -999 }, source, viewport, rotation, scale)).toEqual({
        x: 0,
        y: 0,
      });
    }
  });
  it("allows panning only on overflowing axes and keeps content reachable", () => {
    const source = { width: 1000, height: 300 },
      viewport = { width: 500, height: 400 };
    expect(imagePan({ x: 9999, y: 9999 }, source, viewport, 0, 1)).toEqual({
      x: 250,
      y: 0,
    });
    expect(imagePan({ x: -9999, y: -9999 }, source, viewport, 90, 1)).toEqual({
      x: 0,
      y: -300,
    });
    expect(imageZoom(100)).toBe(8);
    expect(imageZoom(0.01)).toBe(0.25);
  });
  it("provides a named modal, distinct source action and secondary mirror controls", () => {
    const html = renderToStaticMarkup(
      <ImageViewer
        src="data:image/png;base64,aW1hZ2U="
        alt="Fig 2"
        onClose={() => {}}
        onSource={() => {}}
      />,
    );
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('aria-label="图片查看：Fig 2"');
    expect(html).toContain("查看原页");
    expect(html).toContain("向左旋转图片90度");
    expect(html).toContain("向右旋转图片90度");
    expect(html).toContain("<summary>更多调整</summary>");
    expect(html).toContain("水平镜像");
    expect(html).toContain('draggable="false"');
    expect(html).toContain('src="data:image/png;base64,aW1hZ2U="');
    expect(html).not.toContain('<details class="image-viewer-more" open');
  });
});
