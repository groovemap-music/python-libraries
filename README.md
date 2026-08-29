# GrooveMap Python libraries

Private, MIT-licensed Python libraries shared by GrooveMap services. This repository owns
two independently buildable distributions with one synchronized version:

| Distribution | Import surface | Responsibility |
| --- | --- | --- |
| `groovemap-runtime` | `common` | Health, logging, normalization, retries, and database/message-broker resilience |
| `groovemap-agent-tools` | `common.agent_tools` | Framework-neutral catalog query tools used by the API and MCP server |

The runtime's base install is dependency-light. Consumers select the `metrics`, `neo4j`,
`postgres`, or `rabbitmq` extra they need; `all` is intended for development and validation.
Service-specific configuration, OAuth, deployment policy, and extraction state are outside
this repository's boundary.

## Development

Install [mise](https://mise.jdx.dev/) and run:

```bash
mise install
just setup
just check
```

The stable local interface is `just setup`, `just check`, `just test`, and `just build`.
`just test-integration` additionally requires a live RabbitMQ service. `just audit` uses
network vulnerability data and is intentionally separate from the pre-merge gate.

Build artifacts are written to `dist/runtime` and `dist/agent-tools`. To prove that wheels
work independently, run `just install-check`, which creates isolated temporary environments
and imports both packages from their built wheels.

## Versioning and releases

PEP 621 package metadata is the version authority. Commitizen keeps the runtime and
agent-tools versions synchronized and uses annotated `v$version` tags. `just bump-preview`
calculates the next version and changelog without modifying the repository. `just bump`
updates local metadata only; it does not tag, push, publish, or release.

No registry publication is enabled during the migration. A later release design must use
an approved OIDC trusted publisher or an equivalently narrow identity and must build from
the reviewed version tag.

## Consumer boundary

Consumers pin a released version and immutable commit. Development linking is allowed only
through an explicit local uv source; credentials must never appear in dependency URLs,
manifests, lockfiles, Docker arguments, or image layers. See
[private-package-auth.md](private-package-auth.md).

## License and history

The current tree is licensed under the [MIT License](LICENSE). The repository was extracted
from `SimplicityGuy/discogsography` by filtering `main` to `common/`, `tests/common/`,
`tests/test_health_server.py`, and the historical root license, then promoting `common/` to
the repository root and `tests/common/` to `tests/`. This retained 154 relevant commits.
Historical license revisions remain available in Git history; the original monorepo remains
unchanged.

## Documentation

See the [documentation index](docs/README.md) for package boundaries, private dependency
authentication, and source-history provenance.
