# GrooveMap agent tools

`groovemap-agent-tools` provides framework-neutral async query orchestration shared by
`catalog-api` and `mcp-server`. Callers supply resolvers and database handles; this package
does not own database credentials, connections, HTTP routing, or application policy.

The distribution supports Python 3.13 or later, installs no console command, and requires the
exact same version of `groovemap-runtime`. It is owned and released from the GrooveMap
`python-libraries` repository. See the local [agent-tools API contract](../docs/agent-tools.md),
[compatibility and release policy](../docs/compatibility-and-releases.md), and
[documentation index](../docs/README.md).
