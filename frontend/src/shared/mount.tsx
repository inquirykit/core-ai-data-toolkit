// What every page's entry renders around it: the fonts and theme, the query
// cache (shared/query.ts), reduced motion when the reader asks for it,
// tooltips and toasts, and what shows if the page throws while it is drawn
// (shared/boundary.tsx). The query devtools are in `npm run dev` only.
import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { MotionConfig } from "motion/react";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "@/shared/globals.css";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Boundary } from "./boundary";
import { createQueryClient } from "./query";

export function mount(page: ReactNode) {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={createQueryClient()}>
        <MotionConfig reducedMotion="user">
          <TooltipProvider>
            <Boundary what="The page" page>
              {page}
            </Boundary>
            <Toaster />
          </TooltipProvider>
        </MotionConfig>
        <ReactQueryDevtools buttonPosition="bottom-left" />
      </QueryClientProvider>
    </StrictMode>,
  );
}
