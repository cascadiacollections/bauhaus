import { describe, expect, it } from "vitest";

import { classifyUA, getAllowedOrigins, isProgressive, isStrip } from "../src/index";

describe("worker helper utilities", () => {
  it("detects mobile user agents", () => {
    expect(classifyUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")).toBe("mobile");
    expect(classifyUA("Mozilla/5.0 (Linux; Android 14; Pixel 8)")).toBe("mobile");
  });

  it("detects desktop user agents", () => {
    expect(classifyUA("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")).toBe("desktop");
    expect(classifyUA("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)")).toBe("desktop");
  });

  it("parses progressive and strip query flags", () => {
    expect(isProgressive(new URL("https://example.com/api/today?progressive=true"))).toBe(true);
    expect(isProgressive(new URL("https://example.com/api/today"))).toBe(false);
    expect(isStrip(new URL("https://example.com/api/today?strip=true"))).toBe(true);
    expect(isStrip(new URL("https://example.com/api/today"))).toBe(false);
  });

  it("uses configured allowed origins and trims whitespace", () => {
    const env = {
      ALLOWED_ORIGINS: "https://example.com, https://cdn.example.com , https://api.example.com",
    } as Parameters<typeof getAllowedOrigins>[0];

    expect([...getAllowedOrigins(env)]).toEqual([
      "https://example.com",
      "https://cdn.example.com",
      "https://api.example.com",
    ]);
  });

  it("falls back to the default allowed origins when none are configured", () => {
    const env = { ALLOWED_ORIGINS: "" } as Parameters<typeof getAllowedOrigins>[0];

    expect([...getAllowedOrigins(env)]).toEqual([
      "https://kevintcoughlin.com",
      "https://www.kevintcoughlin.com",
    ]);
  });
});
