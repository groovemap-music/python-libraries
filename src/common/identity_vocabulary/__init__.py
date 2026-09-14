"""Vendored canonical identity vocabulary package data.

``identity-vocabulary.json`` in this directory is vendored verbatim from the ``design``
repository per ADR 0009 (native identity and provider aliases). ``source.json`` records
the design commit, source path, and SHA-256 digest that ``just check`` verifies against
on every run. See the repository README's "Vendored identity and event vocabularies"
section for the vendoring rule.

This package exposes package data only. The helpers that read this vocabulary and mint
and resolve native identities live in ``common.identity``.
"""
