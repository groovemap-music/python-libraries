"""Vendored canonical media taxonomy package data.

``media-taxonomy.json`` in this directory is vendored verbatim from the ``design``
repository per ADR 0007. ``source.json`` records the design commit, source path, and
SHA-256 digest that ``just check`` verifies against on every run. See the repository
README's "Vendored media taxonomy" section for the vendoring rule.

This package exposes package data only. The mapper that reads this vocabulary lives in
``common.media`` (``map_discogs_formats``, ``map_musicbrainz_release``, and friends).
"""
