"""PayPilot - an AI dunning agent that recovers failed subscription payments.

The package is organised around a single LangGraph flow:

* :mod:`app.ingest`  - builds the RAG retriever over ``data/playbook.md``.
* :mod:`app.nodes`   - the five node functions that make up the recovery flow.
* :mod:`app.graph`   - wires the nodes into a ``StateGraph`` and exposes
  :func:`app.graph.run_recovery`.
* :mod:`app.api`     - a thin FastAPI surface over the graph.
"""

__version__ = "0.1.0"
