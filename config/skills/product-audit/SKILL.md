---
name: product-audit
description: Inspect the current AxonFlow repository and runtime, identify evidence-backed product gaps, and turn the highest-value gap into one bounded implementation task. Use for product health reviews, self-optimization runs, backlog proposals, and deciding the next improvement before coding.
---

# Product audit

Inspect before proposing. Read the recent optimization log, then use focused search to inspect only the product promise, implementation, tests, or runtime evidence needed for one candidate. Use `references/audit-checklist.md` to avoid shallow feature-only reviews. Stop inspecting as soon as one defect has two independent evidence points; use at most 12 tool calls.

Select exactly one task that is small enough for one Codex execution and can be verified automatically. Do not edit files. Do not propose broad rewrites, cosmetic-only work, credential changes, deployments, commits, or destructive operations.

Return only one JSON object:

```json
{
  "verdict": "proposed",
  "task": "Concrete implementation instruction",
  "problem": "Observed product problem",
  "evidence": ["file:line, API observation, test gap, or UI observation"],
  "requirements": ["Required behavior"],
  "acceptance_criteria": ["Observable pass condition"],
  "risk": "low|medium",
  "affected_areas": ["repository-relative path or subsystem"],
  "out_of_scope": ["Explicit exclusions"]
}
```

Use `verdict=proposed` only when the evidence is current and the acceptance criteria are testable. If no safe bounded task exists, return `verdict=blocked` and explain why.
