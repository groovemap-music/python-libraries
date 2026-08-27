# Repository instructions

- Keep `groovemap-runtime` dependency-light; optional service integrations belong in named extras.
- Keep `groovemap-agent-tools` framework-neutral and synchronized to the runtime package version.
- Do not add service configuration, OAuth, deployment policy, credentials, or generated authentication files.
- Run `just check` before proposing a change.
- `just bump` may update local files only. Publishing, tagging, pushing, and releasing require separate approval.
