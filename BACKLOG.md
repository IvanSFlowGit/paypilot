
## Backend baseline audit - 2026-07-12
Rubric: backend-engineering-baseline.md. Score: DESIGN green, BUILD green, DATA partial, SECURITY green, DEPLOY green, OPERATE green. Standout repo.
- Add restore-tested persistence or an explicit stateless note; in-memory FAISS + JSON = no durable state [medium]
- Wire an alert/tracing sink to the JSON audit log; metrics are in-proc only, lost on restart [medium]
- Add a ruff lint step to CI [low]
- Add a lessons.md / incident log (only regression snapshots capture past bugs today) [low]
