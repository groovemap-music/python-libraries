# Changelog

All notable changes to the GrooveMap Python libraries will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/).

## Unreleased

- Add `common.media`, the shared mappers for the canonical media taxonomy of ADR 0007:
  `map_discogs_formats`, `map_musicbrainz_release`, `legacy_format_names_to_media`,
  `families_of`, `family_ids`, `medium_ids`, and `medium_label`, proved against the design
  repository's conformance fixtures.
- Vendor the media taxonomy into `groovemap-runtime` as package data, with a digest check.
- Add a `media` filter to the agent tools' `search` and a typed `MediaBlock` pass-through on
  `get_release_details`.
- Extract `groovemap-runtime` and `groovemap-agent-tools` from the GrooveMap monorepo.
