# Python package source authentication transition

**Public-library cutover: complete.** `groovemap-music/python-libraries` is public, and current
consumers resolve both distributions from credential-free HTTPS declarations pinned to immutable
commits. Dependency declarations and `uv.lock` record each resolved commit. No private-package
credential is required for current local, CI, release, or container builds.

## Local development

Install the locked public dependencies directly:

```bash
uv sync --frozen
```

An editable workspace/path source is permitted only in a local development override. It
must not be committed to a service's release manifest, CI configuration, or production
container build.

## Dormant automation compatibility

The shared Automation workflows retain the optional `requires-private-library`,
`private-library-client-id`, `private-library-revision`, and `PRIVATE_LIBRARY_PRIVATE_KEY`
interfaces for compatibility with a separately approved private source. They default off or
empty, and no current GrooveMap caller enables them for `python-libraries`. This dormant surface
is not part of the public-library installation path and must not be configured as a routine
credential requirement.

If that compatibility path is ever explicitly enabled, the provider validates its inputs and
fails closed. Tokens and private keys must remain ephemeral: never place one in a dependency URL,
build argument, environment layer, build context, cache key, label, copied file, or artifact.

## Completed transition evidence

Before publication, a narrowly installed GitHub App supplied temporary access while the
ten-consumer matrix proved that all services passed their complete gates without credentials.
The public visibility change, anonymous-fetch verification, and credential removal were separate
reviewed operations and are now complete. The historical evidence and its original approval
boundary remain in the [consumer compatibility record](docs/consumer-compatibility.md).
