"""Optional, opt-in extension hooks for signoz-mcp.

Nothing here is wired by default. Import and register what you want from your own
startup code (or, for the bundled audit-log hook, see ``server.main`` which registers
it across all tools). See ``contrib/audit_log.py``.
"""
