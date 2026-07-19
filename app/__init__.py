# Copyright (c) 2026 Ivan S (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""PayPilot - an AI dunning agent that recovers failed subscription payments.

The package is organised around a single LangGraph flow:

* :mod:`app.ingest`  - builds the RAG retriever over ``data/playbook.md``.
* :mod:`app.nodes`   - the seven node functions that make up the recovery flow.
* :mod:`app.graph`   - wires the nodes into a ``StateGraph`` and exposes
  :func:`app.graph.run_recovery`.
* :mod:`app.api`     - a thin FastAPI surface over the graph.
"""

# Load .env before any submodule reads the environment (OpenAI + Langfuse keys).
# This runs when the ``app`` package is first imported, i.e. before app.graph /
# app.tracing initialise. load_dotenv does not override real env vars, so Fly
# secrets still win in production.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv absent or unreadable .env: fall back to os env
    pass

__version__ = "0.1.0"
