# Four Postgres bugs that about 2,800 passing checks missed

I added row-level security to PayPilot, a payment-retry agent, on top of Postgres. Every automated check passed, about 2,800 of them. All of them ran against SQLite, or against SQL I had only parsed.

The first run on a real PostgreSQL 15 cluster failed four times, each for a different reason. The schema apply died with permission denied for schema public, because PG15 no longer grants CREATE there by default. Changing a function's owner needed CREATE on the schema for the new owner. setval needs UPDATE on a sequence and I had granted USAGE. And I had written pg_catalog.nullif, which does not exist, because NULLIF is syntax and not a function. My own test insisted on it.

A mock accepts whatever you wrote, so none of mine could fail this way. What found all four was a throwaway cluster with scram-sha-256 logins. The script now exits "could not run" instead of passing when the cluster is missing.

Two changes came out of it. CI parses every emitted statement with pglast, so a syntax error shows up before a server does. And the check script prints every failing schema statement at once, instead of stopping at the first. It passes 52 of 52 on PG15 and PG18. I have not run it on managed Postgres or any other version.

If your tests never touched the real database, the passing count is a statement about your mocks.

The check script is scripts/pg_rls_check.py. What it covers and what it has never been run against is in docs/claims.md, rows 51 to 53.
