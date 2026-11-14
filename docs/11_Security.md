# Security

## Authentication

A GitHub App signs a JWT with its private key, exchanges it for a
short-lived installation token, and uses that. Tokens are cached until
shortly before they expire and never written down.

## Authorisation

The App requests two permissions and nothing else:

| | |
|---|---|
| `pull_requests` | read and write — to read the diff and post comments |
| `contents` | read — to download the repository at the commit under review |

No write access to code, no workflow permission, no administration.

## Webhook security

Every delivery is checked against `X-Hub-Signature-256` before anything
else happens, using `hmac.compare_digest` against the raw bytes — the
parsed and re-encoded body does not match. A failure is logged and
nothing is stored, so unauthenticated input never reaches the database.

The reply to a bad signature is deliberately vague: a forger should learn
nothing from it.

Replay is handled by the unique constraint on `delivery_id` rather than
by rate limiting. The same message twice produces one review and the
second is recorded as a duplicate. There is **no rate limiting** on the
endpoint; an earlier version of this document claimed there was.

## Secrets

The App's private key and the model API keys live in `.env`, which is
gitignored. The config holds a *path* to the key, not the key. No secret
is committed, and the only place a decrypted value could leak is a log
line — which is a real risk, not a theoretical one: the reviewer found
exactly that bug in a test repository, an API key written to the log at
INFO.

## What the model is allowed to do

- No code from a pull request is ever executed.
- Tools read the snapshot only. There is no write tool, no shell, no
  network access from inside the loop.
- Tool names and arguments are validated by the provider before our
  toolbox runs, and a rejected call is corrected rather than obeyed.
- Nothing the model says can trigger an action beyond posting a comment.
  It cannot merge, approve, block, or change a setting.

## Untrusted input, and what does not hold

A pull request's diff, its comments, and **every file the toolbox reads**
are untrusted. They are data, never instructions.

The tool loop widens this rather than narrowing it: an agent reads
arbitrary repository files and feeds them into its own conversation, so a
file containing text addressed to a reviewer reaches the model inside a
tool result.

**What holds:** tool output is bounded and labelled as a result, findings
must cite a line in the diff, a second model from a different family
re-checks each one, and the blast radius of a successful injection is a
wrong comment.

**What does not hold:** there is no explicit instruction-versus-data
separation inside a tool result, and nothing detects a file trying to
address the reviewer. This is a known gap, stated rather than claimed as
solved.

## Auditability

Every finding records the model that produced it, a `prompt_version`
hashed from the prompt text, and whether a specialist or the linter found
it. Every review records its calls, tokens and seconds. A result can be
traced to what produced it.

## The product rule

It comments. It does not block, approve, merge, or change code. A human
makes the merge decision. This is a principle rather than a limitation,
and it is why autofix is out of scope.
