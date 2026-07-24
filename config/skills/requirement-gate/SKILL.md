---
name: requirement-gate
description: Review an AxonFlow product task before coding, validate evidence, value, scope, safety, and testability, then approve it, refine it, request product rework, or stop. Use as a Supervisor quality gate between product analysis and implementation and after implementation or testing results.
---

# Requirement gate

Read `references/review-policy.md`. Treat static workflow routes as suggestions, not proof that the task should proceed.

For a product proposal, require current evidence, one bounded outcome, explicit acceptance criteria, and no hidden deployment or destructive action. Preserve the proposal's structured fields when assigning Codex.

For Codex output, require changed-file evidence and proportionate automated tests before sending to acceptance testing. For test output, finish only when every requirement has a recorded pass; route failures back to Codex with exact evidence.

Supervisor decisions must be a plain JSON object matching the schema requested by the Supervisor prompt. Do not wrap JSON in Markdown fences. Never mark the workflow done merely because a node returned transport-level `status=success`; inspect the business verdict and evidence.
