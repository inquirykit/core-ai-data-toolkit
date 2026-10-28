import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The pages are served by FastAPI from api/static, mounted at /admin/static,
// and opened at these paths (api/admin_routes.py, setup_routes.py,
// playground_routes.py).
const BASE = "/admin/static/";
const PAGES: Record<string, string> = {
  "/admin": "admin.html",
  "/admin/setup": "setup.html",
  "/playground": "playground.html",
};
const BACKEND = process.env.AI_TOOLKIT_URL ?? "http://localhost:8000";
const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

// In `npm run dev`, open the pages at the same paths as in production, so
// the links between them work.
function pagePaths(): Plugin {
  return {
    name: "page-paths",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const path = req.url?.split(/[?#]/)[0];
        if (path && PAGES[path]) req.url = BASE + PAGES[path];
        next();
      });
    },
  };
}

export default defineConfig({
  base: BASE,
  plugins: [react(), tailwindcss(), pagePaths()],
  resolve: { alias: { "@": here("src") } },
  build: {
    outDir: here("../api/static"),
    emptyOutDir: true,
    rollupOptions: {
      input: Object.fromEntries(
        Object.values(PAGES).map((file) => [file.replace(".html", ""), here(file)]),
      ),
    },
  },
  server: {
    // Everything outside BASE (the pages and their modules) goes to the API.
    proxy: {
      "^/(?!admin/static/).*": { target: BACKEND },
    },
  },
});
