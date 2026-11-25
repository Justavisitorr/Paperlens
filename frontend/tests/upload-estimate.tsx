// Isolated browser regression harness. It never submits jobs or contacts a model.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { UploadPage } from "../src/UploadPage";
import "../src/styles/index.css";

const pending: {
  id: number;
  name: string;
  mode: string;
  signal: AbortSignal | null | undefined;
  resolve: (response: Response) => void;
}[] = [];
const nativeFetch = window.fetch;
window.fetch = (input, init) => {
  const path = String(input);
  if (path.endsWith("/api/processing-profiles"))
    return Promise.resolve(
      new Response(
        JSON.stringify({
          default: "standard",
          profiles: ["economy", "standard", "detailed"].map((id, index) => ({
            id,
            label: ["经济", "标准", "精细"][index],
            description: "阅读全文并核查证据。",
            limits: {
              max_requests: 120,
              vision_pages: 0,
              vision_enabled: false,
              math_enabled: false,
            },
          })),
        }),
        { headers: { "content-type": "application/json" } },
      ),
    );
  if (path.endsWith("/api/estimates")) {
    const body = init!.body as FormData;
    const file = body.get("file") as File | null;
    return new Promise((resolve) =>
      pending.push({
        id: pending.length,
        name: file?.name || `pages:${body.get("pages") || "assumed"}`,
        mode: body.get("speed_mode") as string,
        signal: init?.signal,
        resolve,
      }),
    );
  }
  if (path.includes("/api/")) return Promise.reject(new Error(`Unexpected API: ${path}`));
  return nativeFetch(input, init);
};

const files = { a: new File(["PDF A"], "alpha.pdf"), b: new File(["PDF B"], "beta.pdf") };
function Harness() {
  const [staged, setStaged] = useState<File[]>([files.a]);
  const [active, setActive] = useState(true);
  return (
    <>
      <nav aria-label="回归控制">
        <button onClick={() => setStaged([files.a])}>选择 A</button>
        <button onClick={() => setStaged([files.b])}>选择 B</button>
        <button onClick={() => setActive(false)}>离开上传页</button>
        <button onClick={() => setActive(true)}>返回上传页</button>
      </nav>
      <UploadPage
        active={active}
        staged={staged}
        jobs={[]}
        jobsSource="server"
        sessionJobs={new Set()}
        busy={false}
        error=""
        canUpload
        onPick={() => {}}
        onDropFiles={() => {}}
        onUnstage={(index) => setStaged((old) => old.filter((_, i) => i !== index))}
        onStart={() => {
          throw new Error("This check must not submit jobs");
        }}
        onAction={() => {}}
      />
    </>
  );
}

Object.assign(window, {
  uploadEstimateCheck: {
    requests: () =>
      pending.map(({ id, name, mode, signal }) => ({
        id,
        name,
        mode,
        aborted: !!signal?.aborted,
      })),
    finish: (id: number, seconds: number, pages = 8, failed = false) => {
      const range = { low: seconds, typical: seconds, high: seconds };
      pending[id].resolve(
        new Response(
          JSON.stringify(
            failed
              ? { detail: "Fixture failure" }
              : {
                  pages,
                  pages_assumed: false,
                  model: "local-fixture",
                  history_runs: 0,
                  chars_per_token: 2,
                  price_checked_at: null,
                  price_stale: false,
                  profiles: ["economy", "standard", "detailed"].map((profile, index) => ({
                    profile,
                    label: ["经济", "标准", "精细"][index],
                    samples: 0,
                    same_profile: false,
                    ceiling: null,
                    typical: {
                      requests: range,
                      prompt_tokens: range,
                      completion_tokens: range,
                      seconds: range,
                      cost: {
                        low: { status: "unknown" },
                        typical: { status: "unknown" },
                        high: { status: "unknown" },
                      },
                    },
                  })),
                },
          ),
          { status: failed ? 503 : 200, headers: { "content-type": "application/json" } },
        ),
      );
    },
  },
});
createRoot(document.getElementById("root")!).render(<Harness />);
