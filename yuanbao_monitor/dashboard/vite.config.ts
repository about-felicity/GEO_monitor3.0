import vinext from "vinext";
import { defineConfig } from "vite";
import hostingConfig from "./.openai/hosting.json";
import { sites } from "./build/sites-vite-plugin";

const SITE_CREATOR_PLACEHOLDER_DATABASE_ID =
  "00000000-0000-4000-8000-000000000000";

const { d1, r2 } = hostingConfig;

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";

const localBindingConfig = {
  main: "./worker/index.ts",
  compatibility_flags: ["nodejs_compat"],
  d1_databases: d1
    ? [
        {
          binding: d1,
          database_name: "site-creator-d1",
          database_id: SITE_CREATOR_PLACEHOLDER_DATABASE_ID,
        },
      ]
    : [],
  r2_buckets: r2
    ? [
        {
          binding: r2,
          bucket_name: "site-creator-r2",
        },
      ]
    : [],
};

export default defineConfig(async () => {
  // Keep Wrangler and Miniflare state project-local. These are non-secret tool
  // settings; application environment belongs in ignored `.env*` files.
  process.env.WRANGLER_WRITE_LOGS ??= "false";
  process.env.WRANGLER_LOG_PATH ??= ".wrangler/logs";
  process.env.MINIFLARE_REGISTRY_PATH ??= ".wrangler/registry";

  // The local Windows dashboard does not need a Workers runtime. Keeping
  // Miniflare out of local development also avoids requiring a system-wide
  // Visual C++ runtime update. Production builds still use Cloudflare.
  const localNodeDev = process.env.DOUBAO_LOCAL_NODE_DEV === "1";
  const cloudflarePlugins = localNodeDev
    ? []
    : [
        (await import("@cloudflare/vite-plugin")).cloudflare({
          viteEnvironment: { name: "rsc", childEnvironments: ["ssr"] },
          config: localBindingConfig,
        }),
      ];

  return {
    server: {
      ...(isCodexSeatbeltSandbox
        ? { watch: { useFsEvents: false, usePolling: true } }
        : {}),
      // Vite 8 auto-enables browser-console forwarding when it detects an
      // agent. During a dev-server restart the forwarding transport can send
      // before its WebSocket exists, producing the recursive "reading send"
      // rejection overlay seen in the dashboard. Local logs remain available
      // in the browser and terminal without this forwarding bridge.
      forwardConsole: false,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8765",
          changeOrigin: true,
        },
      },
    },
    plugins: [
      vinext(),
      sites(),
      ...cloudflarePlugins,
    ],
  };
});
