"""Vendored canonical company-role vocabulary and block schema package data.

``company-roles.json``, ``company-roles.schema.json``, and ``company-block.schema.json``
in this directory are vendored verbatim from the ``design`` repository per ADR 0011
(catalog identifiers and company roles). ``source.json`` records the design commit,
source path, and SHA-256 digest for each file that ``just check`` verifies against on
every run. See the repository README's "Vendored company-role vocabulary" section for the
vendoring rule.

This package exposes package data only. The closed set and the block validation that read
these files live in ``common.identifiers``.
"""
