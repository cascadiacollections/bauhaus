import { afterEach, describe, expect, it, vi } from "vitest";

import { commitFiles } from "../src/git-commit";
import {
  datesToRecord,
  historyDays,
  historyPaths,
  recordedThrough,
  recordHistory,
  type HistoryEnv,
} from "../src/history";

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

const REMOTE = "https://acct.artifacts.cloudflare.net/git/default/bauhaus-history.git";

/** A receive-pack response that accepts the push, as pkt-lines. */
function acceptedPush(): Response {
  const pkt = (s: string) => `${(s.length + 4).toString(16).padStart(4, "0")}${s}`;
  return new Response(`${pkt("unpack ok\n")}${pkt("ok refs/heads/main\n")}0000`);
}

type Head = { hash: string; treeHash: string; message?: string };

function makeArtifacts(head: Head | null = null) {
  const repo = {
    log: vi.fn(async () => (head ? [head] : [])),
    readTree: vi.fn(async (_hash: string): Promise<ArtifactsTreeEntry[] | null> => null),
    info: vi.fn(async () => ({ remote: REMOTE })),
    createToken: vi.fn(async () => ({ plaintext: "art_test_token" })),
    [Symbol.dispose]: vi.fn(),
  };
  const artifacts = { get: vi.fn(async () => repo) } as unknown as Artifacts;
  return { artifacts, repo, handle: repo as unknown as ArtifactsRepo };
}

function makeBucket(objects: Record<string, string>): R2Bucket {
  return {
    get: vi.fn(async (key: string) =>
      key in objects ? { arrayBuffer: async () => new TextEncoder().encode(objects[key]).buffer } : null,
    ),
  } as unknown as R2Bucket;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// commitFiles — object encoding must match real Git byte for byte
// ---------------------------------------------------------------------------

describe("commitFiles", () => {
  // Expected hashes come from running `git -c commit.gpgsign=false commit` on
  // the same files with GIT_AUTHOR_DATE/GIT_COMMITTER_DATE="1750000000 +0000".
  // "06-notes" next to the "06" directory pins Git's tree ordering, which
  // sorts a directory as if its name ended in "/".
  const files = {
    "2025/06/m.json": '{"a":1}\n',
    "2025/06-notes": "x\n",
    "README.md": "r\n",
  };
  const author = { name: "bauhaus", email: "bauhaus@cascadiacollections.com" };

  it("produces the same commit and tree hashes as git", async () => {
    vi.useFakeTimers({ now: 1_750_000_000_000, toFake: ["Date"] });
    const fetchMock = vi.fn(async () => acceptedPush());
    vi.stubGlobal("fetch", fetchMock);
    const { handle } = makeArtifacts();

    const result = await commitFiles(handle, files, "Test", author);

    expect(result).toEqual({ commit: "80762c713597b71a7c5847ca38e7545daf5bfd1d", parent: null });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${REMOTE}/git-receive-pack`);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer art_test_token");
    const body = new TextDecoder().decode(init.body as Uint8Array);
    expect(body).toContain(`${"0".repeat(40)} 80762c713597b71a7c5847ca38e7545daf5bfd1d refs/heads/main\0report-status`);
    expect(body).toContain("0000PACK");
  });

  it("skips the push when the tree already matches HEAD", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const head = { hash: "a".repeat(40), treeHash: "9aeb1ad3aa3ed3bc2b44c934865c6c3f9d25dbd0" };
    const { handle, repo } = makeArtifacts(head);
    // Serve the existing tree so the rebuilt one hashes identically.
    repo.readTree.mockImplementation(async (hash: string) => {
      const trees: Record<string, ArtifactsTreeEntry[]> = {
        [head.treeHash]: [
          { name: "2025", mode: "40000", hash: "aa298042077e7dfea0f3fe4af03812a118a7022b", type: "tree" },
          { name: "README.md", mode: "100644", hash: "4286f428e3b19fe84de503916ce0e7dc8deefea1", type: "blob" },
        ],
      };
      return trees[hash] ?? null;
    });

    const result = await commitFiles(handle, { "README.md": "r\n" }, "Noop", author);

    expect(result).toEqual({ commit: null, parent: head.hash });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws when the server rejects the ref update", async () => {
    const pkt = (s: string) => `${(s.length + 4).toString(16).padStart(4, "0")}${s}`;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(`${pkt("unpack ok\n")}${pkt("ng refs/heads/main fetch first\n")}0000`)));
    const { handle } = makeArtifacts();

    await expect(commitFiles(handle, files, "Test", author)).rejects.toThrow(
      /Push rejected: unpack ok \| ng refs\/heads\/main fetch first/,
    );
  });

  it("rejects paths that escape the repository", async () => {
    const { handle } = makeArtifacts();
    await expect(commitFiles(handle, { "../x": "y" }, "m", author)).rejects.toThrow(/Invalid repository path/);
    await expect(commitFiles(handle, { ".git/config": "y" }, "m", author)).rejects.toThrow(/Invalid repository path/);
  });
});

// ---------------------------------------------------------------------------
// history helpers
// ---------------------------------------------------------------------------

describe("historyPaths", () => {
  it("maps a date's R2 text objects to per-day repository paths", () => {
    expect(historyPaths("2025-06-15")).toEqual([
      ["metadata/2025/06/15.json", "2025/06/15/metadata.json"],
      ["metadata/2025/06/15.json.sig", "2025/06/15/metadata.json.sig"],
      ["manifests/2025/06/15.json", "2025/06/15/manifest.json"],
    ]);
  });
});

describe("recordedThrough", () => {
  it("reads the newest recorded date from a history commit message", () => {
    expect(recordedThrough("Record history through 2026-10-03")).toBe("2026-10-03");
  });

  it("returns null for an empty repo or any other message", () => {
    expect(recordedThrough(undefined)).toBeNull();
    expect(recordedThrough("Initial commit")).toBeNull();
  });
});

describe("datesToRecord", () => {
  const now = new Date("2026-10-02T11:30:00Z");

  it("returns the window's UTC dates oldest first, crossing a month boundary", () => {
    expect(datesToRecord(now, null, 4)).toEqual(["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]);
  });

  it("skips dates already recorded", () => {
    expect(datesToRecord(now, "2026-09-30", 7)).toEqual(["2026-10-01", "2026-10-02"]);
    expect(datesToRecord(now, "2026-10-02", 7)).toEqual([]);
  });
});

describe("historyDays", () => {
  const env = (days?: string) => ({ HISTORY_DAYS: days }) as HistoryEnv;

  it("defaults to a week", () => {
    expect(historyDays(env())).toBe(7);
  });

  it("rejects values that are not positive integers", () => {
    expect(() => historyDays(env("0"))).toThrow(/HISTORY_DAYS/);
    expect(() => historyDays(env("abc"))).toThrow(/HISTORY_DAYS/);
  });
});

describe("recordHistory", () => {
  const now = new Date("2026-10-04T11:30:00Z");
  const published = {
    "metadata/2026/10/02.json": "{}",
    "metadata/2026/10/03.json": "{}",
    "manifests/2026/10/03.json": "{}",
    "metadata/2026/10/04.json": "{}",
  };

  function makeEnv(head: Head | null) {
    const { artifacts, repo } = makeArtifacts(head);
    const bucket = makeBucket(published);
    const env: HistoryEnv = { BUCKET: bucket, HISTORY: artifacts, HISTORY_REPO: "bauhaus-history", HISTORY_DAYS: "3" };
    return { env, repo, bucket };
  }

  it("records only dates after the last recorded one", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => acceptedPush()));
    const head = { hash: "a".repeat(40), treeHash: "b".repeat(40), message: "Record history through 2026-10-02" };
    const { env, repo } = makeEnv(head);

    const result = await recordHistory(env, now);

    expect(result.dates).toEqual(["2026-10-03", "2026-10-04"]);
    expect(result.parent).toBe(head.hash);
    expect(result.commit).toMatch(/^[0-9a-f]{40}$/);
    // The head read up front is reused, not fetched again by commitFiles.
    expect(repo.log).toHaveBeenCalledTimes(1);
  });

  it("costs one log call and no R2 reads when everything is recorded", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const head = { hash: "a".repeat(40), treeHash: "b".repeat(40), message: "Record history through 2026-10-04" };
    const { env, repo, bucket } = makeEnv(head);

    expect(await recordHistory(env, now)).toEqual({ commit: null, parent: head.hash, dates: [] });
    expect(repo.log).toHaveBeenCalledTimes(1);
    expect(bucket.get).not.toHaveBeenCalled();
    expect(repo.readTree).not.toHaveBeenCalled();
    expect(repo.createToken).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("backfills the whole window into an empty repo", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => acceptedPush()));
    const { env } = makeEnv(null);

    const result = await recordHistory(env, now);

    expect(result.dates).toEqual(["2026-10-02", "2026-10-03", "2026-10-04"]);
    expect(result.parent).toBeNull();
  });
});
