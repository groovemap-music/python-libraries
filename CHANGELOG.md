# Changelog

All notable changes to the GrooveMap Python libraries will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/).

## Unreleased

- Add `common.identity`, the shared native-identity helpers of ADR 0009: `entity_kinds`,
  `catalog_kinds`, `providers`, `alias_sources`, the `is_valid_*` predicates, `new_id`,
  `AliasRef`, `resolve_aliases`, and `attach_aliases`, proved against the design
  repository's conformance fixtures.
- Add `common.events`, the shared first-party-event helpers of ADR 0010: `event_types`,
  `surfaces`, `consent_purposes`, `payload_schema_for`, `is_valid_event_type`, `Event`,
  `Impression`, `EventValidationError`, `validate_event`, `validate_impression`,
  `new_event`, and `new_impression`, proved against the design repository's conformance
  fixtures.
- Vendor the identity vocabulary and event vocabulary, with schemas and fixtures, into
  `groovemap-runtime` as package data from the design repository at commit
  `14aa2d70dded6b0854569cc4909afc5ae4ebe03f`, with digest checks
  (`identity-vocabulary.json` `001db32e91c851d49d252c945c1862e47763462acd44fc6ca84df13bae12e3de`,
  `event-types.json` `f64f03164c208477e8ae9c6bc547bba7342f23df96f6b10f21f7fa6f1d21ba74`).
- Add `common.media`, the shared mappers for the canonical media taxonomy of ADR 0007:
  `map_discogs_formats`, `map_musicbrainz_release`, `legacy_format_names_to_media`,
  `families_of`, `family_ids`, `medium_ids`, and `medium_label`, proved against the design
  repository's conformance fixtures.
- Vendor the media taxonomy into `groovemap-runtime` as package data, with a digest check.
- Add a `media` filter to the agent tools' `search` and a typed `MediaBlock` pass-through on
  `get_release_details`.
- Extract `groovemap-runtime` and `groovemap-agent-tools` from the GrooveMap monorepo.
