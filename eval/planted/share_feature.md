# Planted defects — codebase-chat "share conversations + usage quotas"

Ground truth for a live review run. **Not committed to codebase-chat**: the
reviewer reads that repository at the commit under review, so a label file
inside it would be read as evidence.

28 defects, 5 deliberately clean sections. Severities are what a reviewer
*should* assign, graded by outcome rather than by how hard the fix is.

## Cross-file defects — the premise of the agentic rewrite

These are the ones a single-pass reviewer cannot get. They are listed first
because they are the measurement that matters.

| # | Where | Defect | Severity | Needs |
|---|---|---|---|---|
| X1 | `backend/app/services/retriever.py:147` | Returned key renamed `related_chunks` → `linked_chunks`. The only consumer, `app/routers/chat.py:101`, still reads `context["related_chunks"]` → **KeyError on every chat request**. | high | grep a file **not in the diff** |
| X2 | `backend/app/routers/share.py:157` → `usage.py:89` | User-controlled `sort` query param flows into an f-string in SQL. Injection is only provable by following the call across two files. | high | follow a call |
| X3 | `frontend/src/components/ShareList.jsx:10` | `/share/mine` returns `{items, page}`; this sets state to the object and calls `links.map` → **TypeError**. Shape defined in `share.py:116`. | high | match response shape to consumer |
| X4 | `frontend/src/components/ShareDialog.jsx:57` | Reads `link.expires`; the API returns `expires_at` (`share.py:53`) → renders `Invalid Date`. | medium | match response shape |
| X5 | `backend/app/routers/chat.py:114` | `record_question(db, request.repo_id, user.id, …)` — args transposed against the signature in `usage.py:58`. Usage recorded against the wrong user and repo. The correct call is at `chat.py:41` in the same file. | high | read a signature in another file |

## Security

| # | Where | Defect | Severity |
|---|---|---|---|
| S1 | `sharing.py:21` | Share tokens from `random.choice` (Mersenne Twister), not `secrets`. Tokens are predictable, and they are the only thing protecting a private conversation. | high |
| S2 | `share.py:176` | Decrypted API key written to the log at INFO. | high |
| S3 | `share.py:67` | `revoke_share` never checks `link.user_id != user.id` — any authenticated user can revoke anyone's link. `create_share` and `export_conversation` both check, which proves the omission. | high |
| S4 | `main.py:11` | `allow_origins=["*"]` with `allow_credentials=True`. Credentialed wildcard CORS. Also leaves `settings` imported but unused. | high |
| S5 | `ShareDialog.jsx:33` | `dangerouslySetInnerHTML` on `conversation.title`, which is user input (`chat.py` sets it from `question[:100]`). Stored XSS. | high |
| S6 | `sharing.py:61` | Token compared with `==`, not `secrets.compare_digest`. | low |

## Correctness

| # | Where | Defect | Severity |
|---|---|---|---|
| C1 | `sharing.py:36` | `expires_at` is `DateTime(timezone=True)`, compared against naive `datetime.utcnow()` → `TypeError`. `expiry_from_now` two lines up is correctly tz-aware. | high |
| C2 | `usage.py:19` | `moment - timedelta(days=moment.day - 1)` keeps the time of day, so `period_start` differs every call. `get_or_create_record` never matches an existing row → a new usage record per question, and the quota never binds. | high |
| C3 | `usage.py:49` | `_quota_cache` is keyed on `user_id` only, ignoring `repo_id`, is module-level, shared across requests, and is never invalidated. Wrong quota for a second repo, stale for the process lifetime. | high |
| C4 | `usage.py:80` | `used // limit * 100` is integer division — always 0 until the limit is reached. | medium |
| C5 | `share.py:105` | `offset = page * PAGE_SIZE` with `page` defaulting to 1 skips the entire first page. | medium |
| C6 | `share.py:163` | `percent_used(len(rows))` passes the number of repositories where the function expects questions used. | medium |
| C7 | `export.py:7` | `collect_code_blocks(messages, found=[])` — mutable default accumulates across every call in the process, so one user's export counts another's code blocks. | medium |
| C8 | `chat.py:113` | `tokens_used = len(assistant_content)` counts characters as tokens, ~4x over. | low |
| C9 | `models/share.py:19` | `token` is indexed but **not unique**; it is the lookup key for a shared link. | medium |
| C10 | `alembic/versions/002_share_links.py` | No unique constraint on `usage_records (user_id, repo_id, period_start)`, which is what `get_or_create_record` assumes. | medium |
| C11 | `alembic/env.py` | `app.models.share` is never imported, so the new tables are absent from `target_metadata` and autogenerate would drop them. | medium |

## Error handling

| # | Where | Defect | Severity |
|---|---|---|---|
| E1 | `share.py:85` | `except Exception: pass` around a commit. Loses the write and leaves the session in a failed state for everything after it. | medium |
| E2 | `share.py:91` | `repo.readme_content.strip()` — the column is nullable → `AttributeError` for any repo without a README. | high |
| E3 | `share.py:178` | `httpx.AsyncClient` created without a context manager and never closed. Connection leak per call. | medium |
| E4 | `useShare.jsx:11` | `share` has no try/catch, so a throw leaves `loading` true forever and never sets `error`. `revoke` in the same file uses try/finally correctly. | medium |

## Performance

| # | Where | Defect | Severity |
|---|---|---|---|
| P1 | `sharing.py:59` | `resolve_token` loads **every** non-revoked link and compares in Python instead of a `WHERE token = …`. | medium |
| P2 | `SharedConversation.jsx:15` | `useEffect` with no dependency array, and the effect calls `setData` → infinite fetch loop. | high |
| P3 | `usage.py:101` | Sums `tokens_spent` in Python after loading every row rather than in SQL. | low |

## Maintainability

| # | Where | Defect | Severity |
|---|---|---|---|
| M1 | `SharedConversation.jsx:47` | `<article>` inside `.map()` with no `key`. | low |
| M2 | `SharedConversation.jsx:42` | Reads `data.readme`; the API returns `readme_excerpt` → renders nothing. | low |
| M3 | `useShare.jsx:19` | `useCallback` deps `[]` while closing over `conversationId` → stale closure, always shares the first conversation. `revoke` lists `[link]` correctly. | medium |

## Deliberately clean — false-positive bait

A comment on any of these is a false positive.

| # | Where | Why it is correct |
|---|---|---|
| OK1 | `usage.py:68` `add_tokens_atomically` | Single `UPDATE … SET x = x + n`. Correct under concurrency, and sits in the same file as the racy `record_question`, so a reviewer that flags both is pattern-matching rather than reading. |
| OK2 | `export.py:16` `format_sources` | Correct `json.JSONDecodeError` handling, `.get()` for optional keys, no crash on `None`. |
| OK3 | `lib/api.js` (share helpers) | `encodeURIComponent` on every interpolated token, `res.ok` checked, `.catch` on the error-body parse. |
| OK4 | `ShareBadge.jsx` | Correct `??` default, no unkeyed list, escaped text, no unused state. |
| OK5 | `sharing.py:24` `build_share_url` + `expiry_from_now` | Correct `rstrip` on the base URL, correct tz-aware arithmetic — the contrast that makes C1 provable. |

## Known honest ambiguity

`usage.py:62` `record_question` is a read-modify-write and races under
concurrency (two requests both read N, both write N+1, quota bypassed). It is
listed as a defect, but a reviewer calling it "medium" or "low" rather than
"high" is defensible on a single-process deployment. Counted as found if it is
reported at any severity.

## Scoring

- 28 defects, of which **5 are cross-file** (X1–X5) and are the real test.
- 11 are high severity. A posting floor of 0.6 confidence and a severity gate
  mean not all 28 should post — the summary should say how many were held.
- Any comment on OK1–OK5 counts against precision.
