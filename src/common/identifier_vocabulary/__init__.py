"""Vendored canonical identifier vocabulary and block schema package data.

``identifier-types.json``, ``identifier-types.schema.json``, and
``identifier-block.schema.json`` in this directory are vendored verbatim from the
``design`` repository per ADR 0011 (catalog identifiers and company roles).
``source.json`` records the design commit, source path, and SHA-256 digest for each file
that ``just check`` verifies against on every run. See the repository README's "Vendored
identifier vocabulary" section for the vendoring rule.

This package exposes package data only. The closed sets, the block validation, and the
alias extraction that read these files live in ``common.identifiers``.
"""
