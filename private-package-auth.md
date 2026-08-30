# Python package source authentication transition

The extracted service repositories temporarily consume immutable commits from the private
`groovemap-music/python-libraries` repository. Dependency declarations and `uv.lock` record the
resolved commit. Never place a token, password, or GitHub App private key in the URL.

After this repository becomes public, the same credential-free HTTPS declarations work without a
GitHub App. The exact compatibility evidence and cleanup preconditions are recorded in the
[consumer matrix](docs/consumer-compatibility.md). Do not remove the temporary credentials until
an anonymous HTTPS fetch resolves the reviewed revision and a separate OpenTofu cleanup plan is
approved.

## Local development

Use GitHub CLI's credential helper so Git asks `gh` for the active account at execution
time:

```bash
gh auth status
gh auth setup-git
uv sync --frozen
```

An editable workspace/path source is permitted only in a local development override. It
must not be committed to a service's release manifest, CI configuration, or production
container build.

## GitHub Actions

Cross-repository access uses a narrowly installed GitHub App. Mint a short-lived token
with `actions/create-github-app-token` and expose it only to a credential-helper step.
Store the App identifier as a variable and its private key as an Actions secret. Do not
rewrite dependency URLs with the token, echo it, persist generated credential files, or
upload them as artifacts. Remove the helper and token-bearing environment before later
untrusted steps.

The built-in `GITHUB_TOKEN` is repository-scoped and must not be presented as granting
access to `python-libraries`. A PAT is not the default cross-repository design.

## Container builds

Use BuildKit SSH forwarding with a read-only deploy key or an ephemeral GitHub App token
through a secret mount. The Dockerfile consumes the credential in the same `RUN` layer
that performs `uv sync --frozen` and removes temporary helper state before that layer
finishes. Credentials must never enter `ARG`, `ENV`, build contexts, cache keys, labels,
remote URLs, copied files, or final image layers.

The App variable and Actions/Dependabot secrets remain temporary. Publication, anonymous-fetch
verification, and credential removal are separate reviewed operations; no library validation
command performs them.
