import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    plugins: [react()],
    define: {
      "import.meta.env.NEXT_PUBLIC_API_URL": JSON.stringify(env.NEXT_PUBLIC_API_URL),
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      watch: {
        usePolling: true,
      },
    },
  };
});
