import { describe, expect, it } from "vitest";
import { duration, relative, safeThumbnail } from "./format";

describe("format", () => {
  it("formats durations as m:ss or h:mm:ss", () => {
    expect(duration(0)).toBe("0:00");
    expect(duration(65)).toBe("1:05");
    expect(duration(3599)).toBe("59:59");
    expect(duration(3600 + 2 * 60 + 3)).toBe("1:02:03");
    expect(duration(null)).toBe("");
  });

  it("formats added times relative to now", () => {
    const now = Date.parse("2026-10-30T12:00:00Z");
    expect(relative("2026-10-30T11:59:30Z", now)).toBe("just now");
    expect(relative("2026-10-30T11:15:00Z", now)).toBe("45m ago");
    expect(relative("2026-10-30T07:00:00Z", now)).toBe("5h ago");
    expect(relative("2026-10-27T12:00:00Z", now)).toBe("3d ago");
    expect(relative("", now)).toBe("");
  });

  it("keeps only https thumbnails from YouTube's image hosts", () => {
    expect(safeThumbnail("https://i.ytimg.com/vi/abc/mqdefault.jpg")).toBe("https://i.ytimg.com/vi/abc/mqdefault.jpg");
    expect(safeThumbnail("https://i9.ytimg.com/vi/abc/mq.jpg")).toBe("https://i9.ytimg.com/vi/abc/mq.jpg");
    expect(safeThumbnail("https://yt3.ggpht.com/a/b=s88")).toBe("https://yt3.ggpht.com/a/b=s88");
    expect(safeThumbnail("http://i.ytimg.com/vi/abc/mq.jpg")).toBeNull();
    expect(safeThumbnail("https://evil.example/ytimg.com.jpg")).toBeNull();
    expect(safeThumbnail("https://notytimg.com/x.jpg")).toBeNull();
    expect(safeThumbnail("https://i.ytimg.com.evil.example/x.jpg")).toBeNull();
    expect(safeThumbnail("javascript:alert(1)")).toBeNull();
    expect(safeThumbnail("not a url")).toBeNull();
    expect(safeThumbnail("")).toBeNull();
    expect(safeThumbnail(null)).toBeNull();
  });
});
