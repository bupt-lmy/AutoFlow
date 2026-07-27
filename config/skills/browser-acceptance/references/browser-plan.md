# Browser plan schema

Create a JSON file with a localhost URL and ordered actions:

```json
{
  "url": "http://127.0.0.1:5173/workflows",
  "timeout_ms": 15000,
  "actions": [
    {"type": "expect_text", "text": "Workflows"},
    {"type": "click", "role": "button", "name": "Create Workflow"},
    {"type": "fill", "label": "Workflow name", "value": "Acceptance draft"},
    {"type": "expect_text", "text": "Supervisor"},
    {"type": "screenshot", "path": "artifacts/browser/workflow.png"}
  ]
}
```

Supported actions are `expect_text`, `click`, `fill`, `press`, `wait_for_text`, and `screenshot`. Use accessible roles, names, and labels from the rendered UI. The script stops on the first failed action and emits JSON evidence.
