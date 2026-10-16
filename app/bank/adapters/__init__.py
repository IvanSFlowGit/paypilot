"""Concrete bank adapters. Importing this package registers them.

An adapter lands here only once something has been READ about the provider
it claims to speak for, with the read date and the quote in docs/claims.md.
An adapter written from the shape of an API rather than from its published
specification is a fixture that agrees with its author.
"""

from app.bank.adapters import berlin_group as _berlin_group  # noqa: F401

__all__ = ["_berlin_group"]
