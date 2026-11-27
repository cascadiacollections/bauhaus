import { describe, expect, it } from "vitest";

import { stripJpegMetadata } from "../src/jpeg";

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

function segment(marker: number, payload: Uint8Array): Uint8Array {
  const len = payload.byteLength + 2;
  const out = new Uint8Array(4 + payload.byteLength);
  out[0] = 0xff;
  out[1] = marker;
  out[2] = len >> 8;
  out[3] = len & 0xff;
  out.set(payload, 4);
  return out;
}

const text = (s: string) => new TextEncoder().encode(s);

function sample(): Uint8Array {
  return concat([
    new Uint8Array([0xff, 0xd8]),
    segment(0xe0, text("JFIF\0")),
    new Uint8Array([0xff, 0xff, 0xe1]), // fill byte before APP1
    (() => {
      const payload = text("Exif\0\0GPS:37.7749");
      const len = payload.byteLength + 2;
      return concat([new Uint8Array([len >> 8, len & 0xff]), payload]);
    })(),
    segment(0xed, text("IPTC-SECRET")),
    segment(0xe2, text("ICC_PROFILE")),
    new Uint8Array([0xff, 0xda, 0x00, 0x02]),
    text("SCAN\xff\x00"),
    new Uint8Array([0xff, 0xd9]),
  ]);
}

describe("stripJpegMetadata", () => {
  it("drops APP1 and APP13 and keeps JFIF, ICC, and the scan", () => {
    const out = stripJpegMetadata(sample());
    const decoded = new TextDecoder().decode(out);
    expect(decoded).toContain("JFIF");
    expect(decoded).toContain("ICC_PROFILE");
    expect(decoded).toContain("SCAN");
    expect(decoded).not.toContain("GPS:37.7749");
    expect(decoded).not.toContain("IPTC-SECRET");
    expect(out[0]).toBe(0xff);
    expect(out[1]).toBe(0xd8);
    expect(out[out.byteLength - 2]).toBe(0xff);
    expect(out[out.byteLength - 1]).toBe(0xd9);
  });

  it("returns the same bytes when there is nothing to remove", () => {
    const clean = concat([
      new Uint8Array([0xff, 0xd8]),
      segment(0xe0, text("JFIF\0")),
      new Uint8Array([0xff, 0xd9]),
    ]);
    expect(stripJpegMetadata(clean)).toBe(clean);
  });

  it("returns a non-JPEG unchanged", () => {
    const png = text("\x89PNG not a jpeg");
    expect(stripJpegMetadata(png)).toBe(png);
  });

  it("returns a truncated JPEG unchanged rather than a partial rewrite", () => {
    const broken = concat([
      new Uint8Array([0xff, 0xd8, 0xff, 0xe1, 0x00, 0x20]),
      text("Exif"),
    ]);
    expect(stripJpegMetadata(broken)).toBe(broken);
  });
});
