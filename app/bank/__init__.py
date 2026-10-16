"""The bank seam: read-only account information, by construction.

Read-only is structural here rather than a sentence in a docstring. The
``BankProvider`` protocol declares no method that moves money, so no adapter
can implement one, and ``provider.py`` refuses at import when an adapter
declares an endpoint that matches a payment-initiation deny-list.

Nothing in this package initiates a payment. PIS stays out.
"""
