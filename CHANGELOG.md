# Changelog

All notable changes to the GrooveMap Python libraries will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/).

## Unreleased

- Accept schema-valid MusicBrainz identifier sources in `common.identifiers`, including
  `barcode` and `label-info[].catalog-number`, while keeping company sources Discogs-only.
  MusicBrainz barcode and catalogue-number items mint the same normalized alias namespaces.
- Re-vendor the event vocabulary and identifier vocabulary from the design repository at
  commit `5bfdf1005c5d95c99143e8c2acd189e127e1cb10`, which publishes the `fit` surface and
  its five outcome event types (`fit.shown`, `fit.opened`, `fit.saved`, `fit.dismissed`,
  `fit.hidden`) and admits MusicBrainz as an identifier source, with digest checks
  (`event-types.json` `920a63d1eda5e909e6d6bd4850df005a17e592fbfc6f8f3152a15833f74fa8ad`,
  `identifier-types.json` `87b844a20f35d45e7ba176df58d6ccf3d7bc88beecda90457b1a2afb3432fb34`).
  `common.events.surfaces()` and `common.events.event_types()` now expose the `fit` surface
  and its event types.
- Add `common.identifiers`, the shared catalog-identifier and company-role helpers of
  ADR 0011: `identifier_types`, `alias_identifier_types`, `company_role_categories`,
  `validate_identifiers_block`, `validate_companies_block`, `alias_refs_for_release`, and
  `IdentifierValidationError`, proved against the design repository's conformance
  fixtures.
- Vendor the identifier vocabulary and company-role vocabulary, with schemas and fixtures,
  into `groovemap-runtime` as package data from the design repository at commit
  `d06e1571f6acce6a246e4c0b6be866ecbea44c72`, with digest checks
  (`identifier-types.json` `2df8a691173f779b2d2076f31e8abd01d160e51f4f371de99f1f8e1cb12f26a5`,
  `company-roles.json` `03ab8689ba14768dceffb756a71475c01797caea8d18ba9943cd5f69b3d705de`).
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
