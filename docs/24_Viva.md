# Viva / Project Defense

## Beginner
1. What does this project do?
2. What is a pull request?
3. What is a webhook?
4. What is an LLM?
5. What is a GitHub App?
6. Why use FastAPI?
7. What is a diff?
8. What is Postgres used for here?
9. What is a false positive, in this context?
10. What does "severity" mean for a finding?
11. What is structured output?
12. What is a prompt?
13. What does "judge pass" mean?
14. What is human-in-the-loop?
15. What is a webhook signature?
16. What is CI/CD?
17. What is observability?
18. What is model versioning?
19. Why use open-source repos for evaluation instead of private code?
20. What does "recall" mean here?

## Intermediate
1. Why split review generation and judging across two different models?
2. How do you decide what counts as a duplicate finding?
3. How do you know a finding is actually grounded in the code?
4. How do you evaluate an imbalanced test set (few real bugs, many clean PRs)?
5. How do you prevent hallucinated line numbers?
6. How would you scale connected-file context gathering to a large repo?
7. How do you secure the GitHub App's private key?
8. Why is the pipeline synchronous rather than queued at this scale?
9. How would you add a lightweight dashboard?
10. How do you test the webhook endpoint?
11. How do you track cost per review?
12. How do you handle a failed pipeline stage?
13. Why not use LangChain or a similar framework?
14. What's the difference between HLD and LLD here?
15. What is an API contract, and where is it defined in this project?
16. How do you implement least-privilege permissions for the GitHub App?
17. How would you deploy this to avoid cold starts?
18. What is the purpose of an ADR, and give one from this project?
19. How would you add GitLab support without rewriting the core pipeline?
20. How would you measure whether the feedback loop (v2) is actually working?

## Advanced
1. How would you detect the reviewer's quality drifting over time?
2. How would you design multi-tenant isolation across installations?
3. How would you support multiple LLM providers behind one interface?
4. How would you guarantee every posted finding is grounded, formally?
5. How would you handle an adversarial PR designed to defeat the reviewer?
6. How would you make the review pipeline explainable to a non-technical stakeholder?
7. How would you design a model/prompt registry with rollback?
8. How would you estimate cost at 1,000x current PR volume?
9. How would you design a resilient pipeline if GitHub's API degrades?
10. How would you evaluate human-AI agreement over the feedback loop?
11. How would you decide when targeted search is no longer sufficient and a full code graph is needed?
12. How would you extend this to review across a whole stacked-diff chain, not a single PR?
13. How would you prevent prompt injection from a maliciously crafted commit message?
14. How would you distinguish "the model isn't sure" from "the model has no information"?
15. When would you introduce a queue and worker pool?
16. How would you benchmark this against CodeRabbit/Greptile fairly?
17. How would you design an SLA for review latency at scale?
18. What governance would this need before being trusted with auto-merge?
19. How would you support monorepos with unrelated sub-projects?
20. How would you evolve this from a portfolio project into a funded product?
