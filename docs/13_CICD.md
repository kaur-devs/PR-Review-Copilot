# CI/CD

## Pipeline
```text
Developer
  ↓
Feature Branch
  ↓
Pull Request
  ↓
Lint + Unit Tests
  ↓
Integration/Webhook Tests
  ↓
Security / Dependency Scan
  ↓
Build
  ↓
Deploy to Render (staging)
  ↓
Smoke Test (real test-repo PR)
  ↓
Production/Demo Deployment
```

## Branching
- `main`: stable
- `feature/*`: incremental work

## Pull Requests
Every PR should contain: problem, solution, testing performed, and risks — the same discipline the tool itself enforces on others.

## Quality Gates
Do not merge when required tests fail or secrets are detected.
