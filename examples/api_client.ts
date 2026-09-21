// Call the docket HTTP API from TypeScript (Node 18+, Deno, Bun, or the browser).
// Run: npx tsx examples/api_client.ts invoice.pdf
// Typed clients can be generated from docs/openapi.json, e.g.
//   npx openapi-typescript docs/openapi.json -o docket-api.d.ts
import { readFile } from "node:fs/promises";
import { basename } from "node:path";

const API = process.env.DOCKET_URL ?? "http://localhost:8000";
const headers: Record<string, string> = process.env.DOCKET_API_KEY
  ? { Authorization: `Bearer ${process.env.DOCKET_API_KEY}` }
  : {};

async function extract(path: string) {
  const form = new FormData();
  form.append("file", new Blob([await readFile(path)]), basename(path));

  const submitted = await fetch(`${API}/jobs`, { method: "POST", headers, body: form });
  if (!submitted.ok) throw new Error(`${submitted.status} ${await submitted.text()}`);
  const { job_id } = await submitted.json();

  for (;;) {
    const job = await (await fetch(`${API}/jobs/${job_id}`, { headers })).json();
    if (job.status === "completed") return job.result;
    if (job.status === "failed") throw new Error(job.error);
    await new Promise((r) => setTimeout(r, 2000));
  }
}

const result = await extract(process.argv[2]);
console.log(result.document_type, result.status, result.extracted);
// Each field's page region: result.field_sources[field].bbox = {x0, y0, x1, y1} in 0..1.
if (result.needs_review) console.warn("needs review:", result.review_reasons);
