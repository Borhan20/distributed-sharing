import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api/errors";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { loadRuntimeConfig } from "./auth/runtimeConfig";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Do not retry permission or not-found errors; they will not fix themselves.
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

const root = createRoot(document.getElementById("root")!);
loadRuntimeConfig()
  .then(() =>
    root.render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AuthProvider>
              <App />
            </AuthProvider>
          </BrowserRouter>
        </QueryClientProvider>
      </StrictMode>,
    ),
  )
  .catch((err: unknown) =>
    root.render(
      <main>
        <section className="card narrow" role="alert">
          <h1>Cannot reach the server</h1>
          <p>{err instanceof Error ? err.message : String(err)}</p>
          <p className="muted">Reload the page in a moment. If it keeps happening, tell your server admin.</p>
        </section>
      </main>,
    ),
  );
