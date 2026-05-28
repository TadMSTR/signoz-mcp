module.exports = {
  apps: [{
    name: "signoz-mcp",
    script: "/home/ted/repos/personal/signoz-mcp/.venv/bin/python",
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
      SIGNOZ_QUERY_VERSION: "v3",
      FASTMCP_TRANSPORT: "streamable-http",
      FASTMCP_PORT: "8492",
      FASTMCP_HOST: "127.0.0.1",
    },
  }],
};
