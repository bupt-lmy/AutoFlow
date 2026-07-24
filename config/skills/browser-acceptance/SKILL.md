---
name: browser-acceptance
description: Run automated AxonFlow acceptance tests, operate the local UI in a real headless browser, capture visible evidence, and record every requirement as passed or failed. Use after coding changes, for workflow UI regression checks, and whenever HTTP-only checks cannot verify rendered or interactive behavior.
---

# Browser acceptance

Start from the upstream requirements and Codex evidence. Run the smallest relevant unit/integration tests, then run browser checks for every visible or interactive requirement.

Create a temporary JSON plan following `references/browser-plan.md`, then execute:

```text
@script:browser_acceptance.mjs --plan <repository-relative-plan.json>
```

The script only permits localhost by default. Treat its JSON output and screenshot paths as evidence. Do not claim a browser pass when the script was skipped, unavailable, or returned an error.

Read `docs/optimization/SELF_OPTIMIZATION_LOG.md` if it exists. Append one detailed run section using `references/result-format.md`, preserving all prior content. The report must be written in Chinese; keep commands, code symbols, file paths, URLs, IDs, and raw error excerpts in their original form. Record every requirement individually, including its source, verification procedure, expected result, actual result, evidence, and passed/failed/blocked status. Clearly separate verified facts from inference. If evidence is unavailable, write “未提供” or “未验证” instead of inventing it. Save the report before returning a final verdict.

The report is an audit record, not a short summary. Include the product proposal, reviewer decision and reasoning, Codex implementation details, changed-file purposes, automated test output summaries, browser action sequence, visible assertions, screenshot paths, failures, rework advice, residual risks, and final conclusion. All explanatory prose in both the Markdown report and the returned JSON must be Chinese.

Return only one JSON object:

```json
{
  "verdict": "passed|failed|blocked",
  "summary": "用中文详细说明验证范围与结论",
  "requirements": [{"requirement": "...", "status": "passed|failed", "evidence": ["..."]}],
  "commands": [{"command": "...", "exit_code": 0}],
  "browser_checks": [{"url": "...", "status": "passed|failed", "evidence": ["..."]}],
  "report_path": "docs/optimization/SELF_OPTIMIZATION_LOG.md",
  "failures": []
}
```

Use `passed` only if every requirement passed and the report was saved. Otherwise use `failed`; use `blocked` only for an unavailable required dependency or missing authority.
