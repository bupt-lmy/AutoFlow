# AxonFlow product audit checklist

Check the smallest relevant set of evidence:

1. Product promise: compare `README.md`, `docs/PRD.md`, and the visible UI with implemented behavior.
2. Workflow correctness: inspect prompt assembly, payload mapping, terminal conditions, retries, Supervisor decisions, discovery fallback, and stored run evidence.
3. Operability: inspect health state, scheduling, logs, error visibility, and whether a user can recover without editing YAML.
4. Agent capability: verify that configured tools and Skills can actually perform what the role promises.
5. Test coverage: find untested critical paths, especially structured payloads and failure loops.
6. Scope: prefer one high-impact defect or missing guardrail that can be implemented and verified in a single run.

Reject ideas whose evidence is only speculative, whose acceptance criteria require subjective judgment, or whose implementation would overwrite unrelated work.
