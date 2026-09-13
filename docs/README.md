# Python libraries documentation

- [Repository and package overview](../README.md)
- [Runtime API, configuration, telemetry, and resilient-resource ownership](runtime.md)
- [Agent-tools public API](agent-tools.md)
- [Compatibility and release policy](compatibility-and-releases.md)
- [Historical consumer compatibility and credential-removal evidence](consumer-compatibility.md)
- [Publication-readiness attestation and current release gate](publication-readiness.md)
- [Completed public-library cutover and dormant private-access compatibility](../private-package-auth.md)
- [Source-history provenance](extraction.md)
- [Organization logging emoji vocabulary](https://github.com/groovemap-music/.github/blob/main/docs/emoji-guide.md)

Package behavior is covered by docstrings, contract tests, and independently built distribution
metadata in the root and `agent-tools` `pyproject.toml` files. All repository-local links in
this index resolve within `python-libraries`; the shared emoji vocabulary lives in the public
organization `.github` repository.

**Public-library cutover: complete.** Current consumers use public immutable sources without
private-package credentials; retained compatibility and pre-cutover evidence are labeled
historical or dormant in the linked documents.
