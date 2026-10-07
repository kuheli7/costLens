import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, /api is proxied to the local FastAPI service (uvicorn on :8000).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
