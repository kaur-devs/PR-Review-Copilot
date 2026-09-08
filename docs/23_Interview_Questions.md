# Interview Preparation

## Product Questions
1. Who is the primary user?
2. What problem does this solve that existing tools don't?
3. What is the MVP?
4. Why is human review still required?
5. What is the success metric?
6. What is intentionally out of scope, and why?
7. What happens when the LLM is uncertain?
8. How would you validate this is actually useful to a real team?
9. What would you build next?
10. What is the biggest product risk?

## Technical Questions
1. Why FastAPI?
2. Why a GitHub App instead of a personal access token?
3. Why split Haiku and Sonnet across pipeline stages?
4. Why Postgres over a document store here?
5. Why no agent framework?
6. How is the webhook verified?
7. How is a duplicate webhook delivery handled?
8. How would you scale this to more repos?
9. How do you prevent false-positive fatigue?
10. What does the judge pass actually check?
11. How is model/prompt version tracked?
12. How do you validate structured LLM output?
13. How do you test the review pipeline?
14. How do you handle LLM API failure?
15. How do you monitor cost per review?
16. How do you secure the GitHub App's permissions?
17. Why not use RAG?
18. When would a queue become necessary?
19. When would microservices be justified here?
20. How would you deploy this?

## Architecture Questions
1. Why not let the LLM autofix code directly?
2. How would you support GitLab/Bitbucket in addition to GitHub?
3. How would you add a full code-graph instead of targeted search?
4. How would you scale to thousands of installed repos?
5. How would you detect the reviewer's own quality regressing over time?
6. How would you isolate one installation's data from another's?
7. How would you handle a compromised/malicious PR trying prompt injection?
8. How would you reduce cost at 10x current PR volume?
9. How would you audit every posted finding after the fact?
10. How would you evolve this from a student project into a real product?

## Scenario Questions
1. The LLM claims a bug exists on a line that doesn't match the actual diff. What happens?
2. Two findings on adjacent lines say almost the same thing. What happens?
3. A PR's code comments contain text trying to instruct the reviewer to approve everything. What happens?
4. GitHub's API rate-limits the app mid-review. What's the fallback?
5. The judge pass starts rejecting almost everything. What do you investigate?
6. A team dismisses every finding in one category. What should happen (v2)?
7. The hosting service is cold and a webhook arrives. What happens to that PR?
8. The same PR triggers two webhook deliveries. What happens?
9. Cost per review suddenly triples. What do you check first?
10. A stakeholder wants the bot to auto-merge clean PRs. What has to change before that's reasonable?

## Strong Answer Principle
Explain not only what was built, but why each architectural decision was made and what trade-off was accepted — especially the deliberate choices to skip RAG, autofix, and multi-platform support in v1.
