import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadResponse } from "./browser";

describe("downloadResponse", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("saves the body under its name, as plain bytes whatever its type", async () => {
    const link = { href: "", download: "", click: vi.fn() };
    vi.stubGlobal("document", { createElement: () => link });
    const saved: Blob[] = [];
    vi.stubGlobal("URL", {
      createObjectURL: (blob: Blob) => {
        saved.push(blob);
        return "blob:saved";
      },
      revokeObjectURL: () => {},
    });

    const page = "<!doctype html><script>alert(1)</script>";
    await downloadResponse(new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8" } }), "page.html");

    expect(link.click).toHaveBeenCalledOnce();
    expect(link.download).toBe("page.html");
    expect(link.href).toBe("blob:saved");
    // The object URL is of the page's origin: as text/html it could open as a page beside the key.
    expect(saved[0].type).toBe("application/octet-stream");
    expect(await saved[0].text()).toBe(page);
  });
});
