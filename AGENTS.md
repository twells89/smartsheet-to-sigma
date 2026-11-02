# Smartsheet to Sigma agent entry

Use [`skills/smartsheet-to-sigma/SKILL.md`](skills/smartsheet-to-sigma/SKILL.md)
when the user wants to inventory, assess, or perform a one-time migration of a
Smartsheet sheet, report, dashboard, or workspace into Sigma.

Read only the refs selected by the router before running later scripts. The
skill's local `SKILL.md`, `refs/`, `scripts/`, tests, and requirements are
canonical and must remain usable when the skill folder is copied by itself.

The repository does not install a Sigma MCP server. The agent or user supplies
Sigma tooling and credentials. Never pass Smartsheet or Sigma secrets on a
command line or write them into migration artifacts.
