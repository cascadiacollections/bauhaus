/**
 * Daily Git history of published artwork, kept in a Cloudflare Artifacts repo.
 *
 * R2 holds the served objects; this commits each day's text records — metadata,
 * its signature, and the responsive manifest — so the publish history can be
 * cloned, diffed, and audited with plain Git. Images stay in R2: Artifacts caps
 * a repo at 1 GB, which a year of stylized variants would approach.
 *
 * Kept inside Artifacts' included allowance (10k operations, 1 GB a month):
 * each commit message records the newest date it covers, so a run reads one
 * log entry and only looks at later dates. A day with nothing new costs one
 * log call and a few R2 reads; a day that records costs about eight Artifacts
 * operations. A failed run is caught up by the next, up to HISTORY_DAYS back.
 */

import { commitFiles, readHead, type CommitResult } from "./git-commit";

export interface HistoryEnv {
  BUCKET: R2Bucket;
  HISTORY: Artifacts;
  HISTORY_REPO: string;
  HISTORY_DAYS?: string;
}

const DEFAULT_DAYS = 7;

const AUTHOR = {
  name: "bauhaus",
  email: "bauhaus@cascadiacollections.com",
};

/** R2 key → repository path for one date's text objects. */
export function historyPaths(date: string): Array<[r2Key: string, repoPath: string]> {
  const path = date.replaceAll("-", "/");
  return [
    [`metadata/${path}.json`, `${path}/metadata.json`],
    [`metadata/${path}.json.sig`, `${path}/metadata.json.sig`],
    [`manifests/${path}.json`, `${path}/manifest.json`],
  ];
}

/** The newest date a history commit covers, from its message, or null. */
export function recordedThrough(message: string | undefined): string | null {
  const match = message?.match(/^Record history through (\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : null;
}

/** UTC dates after `after` (exclusive), at most `days` back from `now`, oldest first. */
export function datesToRecord(now: Date, after: string | null, days: number): string[] {
  const dates: string[] = [];
  for (let back = days - 1; back >= 0; back--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - back));
    const date = d.toISOString().slice(0, 10);
    if (after === null || date > after) dates.push(date);
  }
  return dates;
}

export function historyDays(env: HistoryEnv): number {
  const days = Number(env.HISTORY_DAYS ?? DEFAULT_DAYS);
  if (!Number.isInteger(days) || days < 1) {
    throw new Error(`HISTORY_DAYS must be a positive integer, got ${env.HISTORY_DAYS}`);
  }
  return days;
}

export async function recordHistory(
  env: HistoryEnv,
  now: Date,
): Promise<CommitResult & { dates: string[] }> {
  const days = historyDays(env);
  using repo = await env.HISTORY.get(env.HISTORY_REPO);
  const head = await readHead(repo);
  const noChange = { commit: null, parent: head?.hash ?? null, dates: [] };

  const files: Record<string, Uint8Array> = {};
  const dates: string[] = [];
  for (const date of datesToRecord(now, recordedThrough(head?.message), days)) {
    let found = false;
    for (const [key, path] of historyPaths(date)) {
      const obj = await env.BUCKET.get(key);
      if (!obj) continue;
      files[path] = new Uint8Array(await obj.arrayBuffer());
      found = true;
    }
    if (found) dates.push(date);
  }
  if (dates.length === 0) return noChange;

  const message = `Record history through ${dates[dates.length - 1]}`;
  const result = await commitFiles(repo, files, message, AUTHOR, { head });
  return { ...result, dates };
}
