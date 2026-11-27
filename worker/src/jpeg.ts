/**
 * Remove embedded metadata from a JPEG without decoding it.
 *
 * EXIF (including GPS) and XMP live in APP1; IPTC lives in APP13. Those are
 * the segments `?strip=true` promises to drop. APP0 (JFIF), APP2 (ICC) and
 * the entropy-coded scan are kept, so a stripped file still displays in the
 * same colors. Anything that is not a JPEG, or whose markers don't parse,
 * is returned unchanged — a partial rewrite would be a corrupt image.
 */

const SOI = 0xd8;
const EOI = 0xd9;
const SOS = 0xda;
const APP1 = 0xe1;
const APP13 = 0xed;

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.byteLength;
  }
  return out;
}

/** APP1 (EXIF, XMP) and APP13 (IPTC). Everything else stays. */
function dropsMetadata(marker: number): boolean {
  return marker === APP1 || marker === APP13;
}

export function stripJpegMetadata(bytes: Uint8Array): Uint8Array {
  if (bytes.byteLength < 2 || bytes[0] !== 0xff || bytes[1] !== SOI) return bytes;

  const parts: Uint8Array[] = [bytes.subarray(0, 2)];
  let dropped = false;
  let i = 2;

  while (i < bytes.byteLength) {
    if (bytes[i] !== 0xff) return bytes;
    const markerStart = i;
    i += 1;
    // Fill bytes: a marker may be padded with extra 0xFF bytes.
    while (i < bytes.byteLength && bytes[i] === 0xff) i += 1;
    if (i >= bytes.byteLength) return bytes;
    const marker = bytes[i];
    i += 1;

    // Standalone markers (no length). RST0–RST7, TEM, a nested SOI.
    if (marker === EOI || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7) || marker === SOI) {
      parts.push(bytes.subarray(markerStart, i));
      if (marker === EOI) return dropped ? concat(parts) : bytes;
      continue;
    }

    if (i + 1 >= bytes.byteLength) return bytes;
    const segmentLength = (bytes[i] << 8) | bytes[i + 1];
    if (segmentLength < 2 || i + segmentLength > bytes.byteLength) return bytes;

    // SOS: the rest of the file is entropy-coded data, not markers we can
    // walk. Keep it verbatim, including the scan header.
    if (marker === SOS) {
      parts.push(bytes.subarray(markerStart));
      return dropped ? concat(parts) : bytes;
    }

    if (dropsMetadata(marker)) {
      dropped = true;
    } else {
      parts.push(bytes.subarray(markerStart, i + segmentLength));
    }
    i += segmentLength;
  }

  return dropped ? concat(parts) : bytes;
}
