// Commit files to an Artifacts repo without cloning it.
//
// The binding can read commits and trees but not write them, and a clone in a
// Worker pulls every blob at HEAD into memory — fine for a week of history,
// fatal once an archive outgrows 128 MB. Instead this reads only the trees on
// the paths being written (via the binding), builds the new blob/tree/commit
// objects itself, and pushes them as one small packfile over Git smart HTTP.
// Cost is proportional to what changed, not to the size of the repo: one
// readTree per existing directory on a written path, plus info, a token, and
// the push.

export interface CommitAuthor {
  name: string;
  email: string;
}

export interface CommitOptions {
  branch?: string;
  /** The branch head if the caller already read it (null for an empty repo); saves a log call. */
  head?: ArtifactsCommitMetadata | null;
}

export interface CommitResult {
  /** New commit hash, or null when every file already matched HEAD. */
  commit: string | null;
  /** Commit the branch pointed at before the push (null for an empty repo). */
  parent: string | null;
}

const ZERO_OID = "0".repeat(40);
const TREE_MODE = "40000";
const FILE_MODE = "100644";
const TYPE_CODE = { commit: 1, tree: 2, blob: 3 } as const;

type ObjectType = keyof typeof TYPE_CODE;
type GitObject = { type: ObjectType; body: Uint8Array };
type Entry = { mode: string; hash: string };

const encoder = new TextEncoder();

/** The branch's newest commit, or null for an empty repo. */
export async function readHead(repo: ArtifactsRepo, branch = "main"): Promise<ArtifactsCommitMetadata | null> {
  const [head] = await repo.log({ ref: branch, limit: 1 });
  return head ?? null;
}

export async function commitFiles(
  repo: ArtifactsRepo,
  files: Record<string, string | Uint8Array>,
  message: string,
  author: CommitAuthor,
  { branch = "main", head }: CommitOptions = {},
): Promise<CommitResult> {
  if (head === undefined) head = await readHead(repo, branch);
  const parent = head?.hash ?? null;

  const objects = new Map<string, GitObject>();
  const tree = await buildTree(repo, head?.treeHash ?? null, toFileMap(files), objects);
  if (head && tree === head.treeHash) {
    return { commit: null, parent };
  }

  const commit = await addObject(objects, "commit", encodeCommit(tree, parent, message, author));
  const info = await repo.info();
  const token = await repo.createToken("write", 300);
  await receivePack(info.remote, token.plaintext, branch, parent ?? ZERO_OID, commit, [...objects.values()]);
  return { commit, parent };
}

function toFileMap(files: Record<string, string | Uint8Array>): Map<string, Uint8Array> {
  const out = new Map<string, Uint8Array>();
  for (const [path, data] of Object.entries(files)) {
    const parts = path.split("/");
    if (parts.some((p) => p === "" || p === "." || p === ".." || p === ".git")) {
      throw new Error(`Invalid repository path: ${path}`);
    }
    out.set(path, typeof data === "string" ? encoder.encode(data) : data);
  }
  return out;
}

/** Writes `files` (paths relative to this tree) over `treeHash`; returns the new tree hash. */
async function buildTree(
  repo: ArtifactsRepo,
  treeHash: string | null,
  files: Map<string, Uint8Array>,
  objects: Map<string, GitObject>,
): Promise<string> {
  const entries = new Map<string, Entry>();
  for (const e of (treeHash && (await repo.readTree(treeHash))) || []) {
    entries.set(e.name, { mode: e.mode, hash: e.hash });
  }

  const subdirs = new Map<string, Map<string, Uint8Array>>();
  for (const [path, data] of files) {
    const slash = path.indexOf("/");
    if (slash === -1) {
      entries.set(path, { mode: FILE_MODE, hash: await addObject(objects, "blob", data) });
      continue;
    }
    const dir = path.slice(0, slash);
    if (!subdirs.has(dir)) subdirs.set(dir, new Map());
    subdirs.get(dir)!.set(path.slice(slash + 1), data);
  }

  for (const [dir, subfiles] of subdirs) {
    const existing = entries.get(dir);
    const base = existing?.mode === TREE_MODE ? existing.hash : null;
    entries.set(dir, { mode: TREE_MODE, hash: await buildTree(repo, base, subfiles, objects) });
  }

  const hash = await addObject(objects, "tree", encodeTree(entries));
  // An unchanged subtree is already on the server; don't resend it.
  if (hash === treeHash) objects.delete(hash);
  return hash;
}

function encodeTree(entries: Map<string, Entry>): Uint8Array {
  // Git orders tree entries bytewise, comparing a directory as if its name ended in "/".
  const sortKey = (name: string, e: Entry) => encoder.encode(e.mode === TREE_MODE ? `${name}/` : name);
  const sorted = [...entries].sort(([a, ea], [b, eb]) => compareBytes(sortKey(a, ea), sortKey(b, eb)));
  return concat(
    sorted.flatMap(([name, e]) => [encoder.encode(`${e.mode} ${name}\0`), hexToBytes(e.hash)]),
  );
}

function encodeCommit(tree: string, parent: string | null, message: string, author: CommitAuthor): Uint8Array {
  const ident = `${author.name} <${author.email}> ${Math.floor(Date.now() / 1000)} +0000`;
  const lines = [`tree ${tree}`];
  if (parent) lines.push(`parent ${parent}`);
  lines.push(`author ${ident}`, `committer ${ident}`, "", message.endsWith("\n") ? message : `${message}\n`);
  return encoder.encode(lines.join("\n"));
}

async function addObject(objects: Map<string, GitObject>, type: ObjectType, body: Uint8Array): Promise<string> {
  const header = encoder.encode(`${type} ${body.byteLength}\0`);
  const hash = bytesToHex(await sha1(concat([header, body])));
  objects.set(hash, { type, body });
  return hash;
}

async function receivePack(
  remote: string,
  token: string,
  branch: string,
  oldOid: string,
  newOid: string,
  objects: GitObject[],
): Promise<void> {
  const command = pktLine(`${oldOid} ${newOid} refs/heads/${branch}\0report-status\n`);
  const body = concat([command, encoder.encode("0000"), await buildPack(objects)]);

  const res = await fetch(`${remote}/git-receive-pack`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/x-git-receive-pack-request",
      Accept: "application/x-git-receive-pack-result",
    },
    body,
  });
  const lines = parsePktLines(new Uint8Array(await res.arrayBuffer()));
  if (!res.ok) {
    throw new Error(`git-receive-pack failed: HTTP ${res.status} ${lines.join(" | ")}`.trim());
  }
  if (!lines.includes("unpack ok") || !lines.includes(`ok refs/heads/${branch}`)) {
    throw new Error(`Push rejected: ${lines.join(" | ") || "empty response"}`);
  }
}

async function buildPack(objects: GitObject[]): Promise<Uint8Array> {
  const header = new Uint8Array(12);
  header.set(encoder.encode("PACK"));
  const view = new DataView(header.buffer);
  view.setUint32(4, 2);
  view.setUint32(8, objects.length);

  const parts: Uint8Array[] = [header];
  for (const { type, body } of objects) {
    parts.push(packObjectHeader(TYPE_CODE[type], body.byteLength), await deflate(body));
  }
  const pack = concat(parts);
  return concat([pack, await sha1(pack)]);
}

function packObjectHeader(type: number, size: number): Uint8Array {
  const bytes: number[] = [];
  let byte = (type << 4) | (size & 0x0f);
  size = Math.floor(size / 16);
  while (size > 0) {
    bytes.push(byte | 0x80);
    byte = size & 0x7f;
    size = Math.floor(size / 128);
  }
  bytes.push(byte);
  return new Uint8Array(bytes);
}

function pktLine(line: string): Uint8Array {
  const data = encoder.encode(line);
  return concat([encoder.encode((data.byteLength + 4).toString(16).padStart(4, "0")), data]);
}

function parsePktLines(buf: Uint8Array): string[] {
  const decoder = new TextDecoder();
  const lines: string[] = [];
  for (let i = 0; i + 4 <= buf.byteLength; ) {
    const len = parseInt(decoder.decode(buf.subarray(i, i + 4)), 16);
    if (!Number.isFinite(len)) return [decoder.decode(buf).trim()];
    if (len === 0) {
      i += 4;
      continue;
    }
    lines.push(decoder.decode(buf.subarray(i + 4, i + len)).replace(/\n$/, ""));
    i += len;
  }
  return lines;
}

async function deflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data]).stream().pipeThrough(new CompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function sha1(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-1", data));
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.byteLength;
  }
  return out;
}

function compareBytes(a: Uint8Array, b: Uint8Array): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

function hexToBytes(hex: string): Uint8Array {
  return Uint8Array.from(hex.match(/../g)!, (h) => parseInt(h, 16));
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
