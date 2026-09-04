# Compatibility and release policy

## Supported environment

Both distributions require Python 3.14 and are typed (`py.typed`). The supported interpreter
line is Python 3.14, and CI pins that line to Python 3.14.5 through `.mise.toml`.
Future Python lines are not considered supported until a dedicated validation lane passes.
Optional integrations are supported only when installed through the corresponding
`groovemap-runtime` extra.

## Compatibility promises

Until version 1.0, a minor release may change a public API and a patch release must remain
backward-compatible. Starting with version 1.0, the repository follows semantic versioning:
major releases may break public contracts, minor releases add compatible behavior, and patch
releases provide compatible fixes.

The compatibility-controlled surface is:

- every name in `common.__all__` and `common.agent_tools.__all__`;
- documented arguments, return shapes, configuration precedence, health routes, and metrics
  opt-in behavior;
- distribution names, Python requirement, extras, typing marker, and the intentional absence of
  console commands;
- synchronized runtime and agent-tools versions.

Implementation submodules, leading-underscore helpers, deployment policy, consumer settings,
resolver implementations, and application behavior are outside this promise. Deprecations should
be documented for at least one minor release before removal when practical.

Consumers should pin a released version. An immutable commit is acceptable while validating an
unreleased migration, but it is not a substitute for a release. Local development linking belongs
in an explicit uv source and must never place credentials in dependency URLs, manifests,
lockfiles, Docker arguments, or image layers.

## Release process

PEP 621 metadata in the root and `agent-tools/pyproject.toml` files is authoritative. Commitizen
keeps both versions synchronized.

1. Run `just bump-preview` to calculate the next version and changelog without modifying files.
2. Run `just check`, `just audit`, and `just release-dry-run` against the reviewed source. The
   dry run builds both package formats and records checksums, a CycloneDX SBOM, third-party
   notices, and commit-bound local provenance in ignored `dist/` output.
3. With separate approval, run `just bump` to update local version metadata and the changelog.
4. Review and merge the version change.
5. With separate release approval, create the annotated `v$version` tag from the reviewed commit.
6. Build both distributions from that tag and publish through an approved OIDC trusted publisher
   or equivalently narrow identity.

`just bump` does not commit, tag, push, publish, or release. Registry publication remains disabled
during migration. Package publication, Git tags, pushes, and repository visibility changes always
require their own approval.

A version bump is a coordinated step, not a per-change one. Every consumer in the compatibility
matrix requires an exact version (`groovemap-runtime==<version>`) alongside its immutable commit
pin, so raising the version here makes those requirements unsatisfiable until each consumer
raises its pin in the same wave. The consumer-matrix rehearsal proves it: a bump landed on its
own fails every consumer's `install-check` with `no version of groovemap-runtime==<old version>`.
Feature work therefore records itself under `## Unreleased` in the changelog and leaves both
versions alone. Commitizen rewrites that section from the conventional commit subjects when the
coordinated release runs `just bump`, so it is never hand-maintained past a release.

Commitizen's changelog starts at `changelog_start_rev`, the commit before the monorepo
extraction. This repository inherited the monorepo's full history, so an unscoped changelog
would credit these two distributions with dashboards, OAuth flows, and other features they never
contained.
