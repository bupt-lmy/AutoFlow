# Requirement review policy

Approve only when all gates pass:

- Evidence identifies a real behavior, file, test gap, API response, or browser observation.
- Scope is achievable in one Codex run and excludes unrelated cleanup.
- Acceptance criteria are observable and cover regression risk.
- The task stays inside the repository and does not commit, push, publish, expose secrets, or contact external parties.
- The implementation result names changed files and tests actually run.
- Final acceptance records every requirement as `passed` or `failed` with evidence.

Route decisions:

- Weak or speculative proposal → product Agent with requested evidence.
- Approved proposal → Codex Agent with the complete structured proposal.
- Implementation error or failed acceptance → Codex Agent with exact failure evidence.
- Passed acceptance with a saved report → finish.
- Missing authority, credential, or external dependency → abort as blocked; do not invent permission.
