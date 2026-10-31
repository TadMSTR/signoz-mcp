// The venv is built from uv.lock, non-editable, by the install script in README.md
// ("Installation"). Restart by name (`pm2 restart signoz-mcp`): SIGNOZ_API_KEY below is
// read from the shell that first started the app, so `pm2 delete` + `start`, or
// `--update-env`, from a shell without it loses the key.
module.exports = {
  apps: [{
    name: "signoz-mcp",
    script: "/home/ted/repos/personal/signoz-mcp/.venv/bin/python",
    // SECURITY[deferred]: `-m` keeps this checkout first on sys.path for the life of the
    // process; the install script checks the tree only at deploy time. Fix: `-P` (or the
    // entry point), which needs a PM2 redefinition that must keep SIGNOZ_API_KEY.
    // Ticket vikunja#1336. Target: 2027-01-31. Audit: 2026-10-30/venv-advisories-2026-10-p5.
    args: "-m signoz_mcp.server",
    cwd: "/home/ted/repos/personal/signoz-mcp",
    interpreter: "none",

    restart_delay: 5000,
    max_restarts: 10,
    min_uptime: "10s",

    out_file: "/home/ted/logs/signoz-mcp-out.log",
    error_file: "/home/ted/logs/signoz-mcp-error.log",
    log_file: "/home/ted/logs/signoz-mcp.log",
    merge_logs: true,
    time: true,

    env: {
      SIGNOZ_URL: "http://localhost:8080",
      SIGNOZ_API_KEY: process.env.SIGNOZ_API_KEY,
      SIGNOZ_QUERY_VERSION: "v5",
      FASTMCP_TRANSPORT: "streamable-http",
      FASTMCP_PORT: "8492",
      FASTMCP_HOST: "127.0.0.1",
    },
  }],
};
