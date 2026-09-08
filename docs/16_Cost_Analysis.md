# Cost Analysis

## Cost Model
Estimate cost from:
- PRs reviewed per period
- Files reviewed per PR (typically ~5)
- Tokens per file (diff + connected context)
- Classifier calls (Haiku)
- Review generation calls (Sonnet)
- Judge-pass calls (Haiku)
- Hosting (Render free tier)
- Database (Supabase free tier)

## Formula
`Cost per PR ≈ Σ (calls × avg input tokens × input price + calls × avg output tokens × output price)` across classifier, review-generation, and judge-pass stages.

## Actual Estimates (current pricing, per PR ~5 files)
- Claude Haiku 4.5: $1 / $5 per million input/output tokens.
- Claude Sonnet: $2 / $10 per million input/output tokens.
- **Estimated cost per PR review: $0.03–$0.08**, depending on model split and PR size.
- **Estimated total build cost across the 8-week project (development, debugging, eval harness runs): $30–$70**, entirely LLM API usage.
- Hosting (Render free tier) and database (Supabase free tier): **$0**, with documented tradeoffs (cold starts, auto-pause after inactivity).

## Optimization
- Review only changed hunks + targeted connected files, never full files.
- Use the cheaper model (Haiku) for classification and judging; reserve the stronger model (Sonnet) for review generation only.
- Skip binary files, lockfiles, and generated code entirely.
- Track cost per review from week 1, not retroactively.
