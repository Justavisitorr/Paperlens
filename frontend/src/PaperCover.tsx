import { useEffect, useRef, useState } from "react";
import type { RenderTask } from "pdfjs-dist";
import { Icon } from "./icons";
import { sourceDocument } from "./pdfRuntime";
import type { PaperSummary } from "./api";
import type { DashboardReport } from "./types";
import { AssetImage } from "./AssetImage";

/** Always the actual first page; the standalone PDF uses the reader's shared proxy. */
export function PaperCover({
  paper,
  large = false,
}: {
  paper: PaperSummary;
  large?: boolean;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [broken, setBroken] = useState(false);
  const [ready, setReady] = useState(false);
  const thumbnail = paper.thumbnail_url || "";
  const pdf = paper.pdf_url || "";
  const renderPdf = (!thumbnail || broken) && pdf.startsWith("data:application/pdf;");
  useEffect(() => setBroken(false), [thumbnail]);
  useEffect(() => {
    setReady(false);
    if (!renderPdf) return;
    let cancelled = false;
    let task: RenderTask | undefined;
    // sourceDocument only needs the embedded PDF and owns its reference count.
    let retained: ReturnType<typeof sourceDocument>;
    try {
      retained = sourceDocument({
        source_pdf: { data_url: pdf, sha256: "" },
      } as DashboardReport);
    } catch {
      return;
    }
    void retained.document
      .then(async (document) => {
        const page = await document.getPage(1);
        if (cancelled || !canvas.current) return;
        const element = canvas.current;
        const width = large ? 220 : 100;
        const viewport = page.getViewport({
          scale: width / page.getViewport({ scale: 1 }).width,
        });
        const ratio = Math.min(globalThis.devicePixelRatio || 1, 2);
        element.width = Math.ceil(viewport.width * ratio);
        element.height = Math.ceil(viewport.height * ratio);
        task = page.render({
          canvas: element,
          viewport,
          transform: [ratio, 0, 0, ratio, 0, 0],
        });
        await task.promise;
        if (!cancelled) setReady(true);
      })
      .catch(() => {
        /* The paper remains accessible if its cover cannot render. */
      });
    return () => {
      cancelled = true;
      task?.cancel();
      retained.release();
    };
  }, [pdf, renderPdf, large]);
  return (
    <span className={`library-cover ${large ? "is-large" : ""}`} aria-hidden="true">
      {thumbnail && !broken ? (
        <AssetImage
          src={thumbnail}
          alt=""
          loading="lazy"
          onError={() => setBroken(true)}
          onAssetError={() => setBroken(true)}
        />
      ) : renderPdf ? (
        <>
          <canvas ref={canvas} className={ready ? "is-ready" : ""} />
          {!ready && <Icon name="file" size={large ? 34 : 20} />}
        </>
      ) : (
        <Icon name="file" size={large ? 34 : 20} />
      )}
    </span>
  );
}
