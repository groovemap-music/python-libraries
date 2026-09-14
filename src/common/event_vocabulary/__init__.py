"""Vendored canonical event vocabulary and schema package data.

``event-types.json``, ``event-envelope.schema.json``, and ``impression.schema.json`` in
this directory are vendored verbatim from the ``design`` repository per ADR 0010
(first-party events, consent, and deletion). ``source.json`` records the design commit,
source path, and SHA-256 digest for each file that ``just check`` verifies against on
every run. See the repository README's "Vendored identity and event vocabularies"
section for the vendoring rule.

This package exposes package data only. The envelope models and stdlib schema
validation that read these files live in ``common.events``.
"""
