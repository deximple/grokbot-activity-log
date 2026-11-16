# Specialist review K12 — 2026-11-16

**Area:** privacy and redaction: coverage in every record field, capture paths (browser history, transcripts, shell hook), export / report / rollup outputs, error messages and `--json`, false positives that destroy evidence, `--no-redact` semantics.

**Method:** code reading of `gblog.py` as it is in this tree (`__version__ = "0.6.1"`), the tests and the documents. Nothing was run. Every reproduction below was traced by hand through `redact()`, `_redact_env_names()`, `_stored_value()`, `archive_file()` and `capture_browser()`; run each one before fixing.

**Not repeated here:** K1–K11, round 4, and the gaps `SECURITY.md` already lists under "Not covered" (for example `mysql -phunter2`, `PASS=…`, `curl -u`, values cut at `&`).

**Compatibility:** no finding changes the record format or the hash rule. Fixes change only what *new* records and archives contain. Where a fix changes stdout or an existing test, the finding says so.

## Summary

| Id | Severity | Finding |
|---|---|---|
| K12-01 | High | Three value patterns run to the next white space and swallow the rest of a compact-JSON transcript line; a fourth eats the backslash of `\"` and makes the line invalid JSON |
| K12-02 | High | One NUL byte in the first 4 KiB switches secret scanning off for the whole archive (an implicit `--no-redact`); UTF-16 exports are stored verbatim |
| K12-03 | High | The `sk-` / `xai-` / `sk_test_` rules redact ordinary hyphenated names in every text field; D6-01 fixed only the archive `path` |
| K12-04 | Medium | Assignment rules destroy common non-secrets: `$PWD:/path`, `PWD=`, token counters, status words, `bearer <word>`, the flag after a bare `--password` |
| K12-05 | Medium | Gaps inside shapes the documents list as covered: PGP key blocks, URL userinfo with an empty user or password, other `Authorization:` schemes, `password_confirmation` JSON keys |
| K12-06 | Medium | Browser capture stores the full URL path (capability links, `;jsessionid=…`, e-mail addresses) and has no switch for titles |
| K12-07 | Medium | New rules never reach what is already stored; reports re-publish old text; no command finds it |
| K12-08 | Medium | Archives stored without scanning are invisible downstream; `--no-redact` prints nothing |
| K12-09 | Low | `--jay-approval-id` and `--task-id` are silently redacted instead of refused; no notice when `--target` / `--detail` was changed |
| K12-10 | Low | The shell hook logs here-document bodies (file contents) as the command |
| K12-11 | Low | Edges: three stderr texts skip `redact()`; home-directory paths are committed in `state/<bot>.json`; a killed capture leaves a history copy in TMPDIR |
| K12-12 | Low | No PII handling at all (recipients, titles); proposed as an opt-in mask mode |

---

## K12-01 (High) — Value patterns cross JSON string boundaries and destroy the rest of a transcript line

**Where:** `gblog.py`, `_SECRET_PATTERNS` and `_ENV_TAIL_RE`.

```python
(re.compile(r"(?i)([?&](?:token|access_token|…|auth|code)=)[^&\s]+"), r"\1[REDACTED]"),
(re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),  # userinfo in URLs
(re.compile(r"(?i)(--(?:password|passwd|token|secret|api[_-]?key)[ =]+)(?:\"[^\"]*\"|'[^']*'|\S+)"), r"\1[REDACTED]"),
# prefixed-flag rule: same value part, ends in \S+
_ENV_TAIL_RE = re.compile(r"(?i)\s*[:=]\s*(?:\"[^\"\n]{1,}\"|'[^'\n]{1,}'|[^\s'\"&]{6,})")
```

**Problem.** The main archive input is a JSONL transcript, often written compactly (no white space between fields).

- The query rule (`[^&\s]+`), the userinfo rule (second class `[^/\s@]+`) and both flag rules (`\S+`) do not stop at a quote. One match runs through the closing quote of the JSON string and takes every following field up to the next space.
- The unquoted alternative of the assignment rules stops at `"` but consumes a `\` directly before it. Inside a JSON string that removes the escape of `\"`.

The stored evidence loses fields that held no secret, and the line no longer parses. `tests/test_redaction_extended.py` has no compact-JSON case for these rules.

**Reproduction** (`redact()` on each line; the same happens inside `gblog archive`):

| Input line | Stored |
|---|---|
| `{"role":"tool","text":"GET https://x.example/cb?code=abc123","ok":true}` | `{"role":"tool","text":"GET https://x.example/cb?code=[REDACTED]` |
| `{"endpoint":"http://localhost:8080","owner":"jay@example.com"}` | `{"endpoint":"http://[REDACTED]@example.com"}` (no secret was present; `owner` is gone) |
| `{"cmd":"deploy --token abc123","rc":0}` | `{"cmd":"deploy --token [REDACTED]` |
| `{"tool":"bash","input":"echo \"DB_PASSWORD=hunter2hunter2\" >> .env"}` | `…echo \"DB_PASSWORD=[REDACTED]" >> .env"}` (invalid JSON: the `\` is gone) |

**Fix.** Define one "unquoted value character" and use it in all five places:

```python
# Not white space, not a quote, not "&"; a backslash only when it does not start an escaped quote
# (the text may be the inside of a JSON string).
_VAL = r"(?:[^\s'\"&\\]|\\(?![\"']))"
```

1. Query rule: value `[^&\s\"'<>\\]+`.
2. Userinfo rule: both classes also exclude `"`, `'`, `<`, `>`, `\`. (K12-05 changes the same rule for empty parts.)
3. Both flag rules: replace `\S+` with `(?:[^\s\"'\\]|\\(?![\"']))+`, and add a third alternative for an escaped-quote value, `\\\"[^\"\n]*?\\\"`. Without it `--password \"my pass\"` would not be redacted at all.
4. `_ENV_TAIL_RE`, rule 2 (`\b(token|…)\b`) and the glued-name rule: unquoted alternative becomes `_VAL{6,}`.
5. Update the `former` oracle pair of `_LinearRule` in the same commit, so the differential test in `tests/test_k3_k4.py` compares like with like.

Optional second step, safe side only: in `_redact_env_names`, when the matched unquoted value contains a JSON line-break escape (`\n`, `\r`, `\t` as two characters) after at least 6 characters, end the redacted span there. Today `"output":"TOKEN=abc123456\nSTATUS=ok\nRESULT=pass"` loses all three lines.

**Tests** (`tests/test_redaction_extended.py`, new class `JsonBoundaryTest`):

- For each of the four inputs above: `json.loads(redact(line))` succeeds; the secret is gone; `"ok": true` / `"rc": 0` / `"owner"` survive. Row 2 is returned unchanged.
- `redact('--password \\"my pass\\" x')` contains neither `my` nor `pass`.
- Every existing case of `ALL_CASES` and `test_every_documented_query_parameter` still passes.
- The idempotency loop runs over the new samples.
- `archive_file()` of a three-line compact JSONL transcript: every stored line parses.

---

## K12-02 (High) — A NUL byte is an implicit `--no-redact`

**Where:** `gblog.py`, `_is_text()` and `archive_file()`.

```python
def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
…
as_text = _is_text(data)
unscanned = bool(do_redact) and not as_text and b"\x00" in data
if unscanned:
    sys.stderr.write("warning: binary/non-UTF-8 content stored without secret scanning\n")
```

**Problem.**

- A:L-5 fixed "one invalid byte switches scanning off". One NUL byte still does: the content is stored verbatim, the command exits 0, and the only signals are a stderr line and `scanned: false`.
- The README allow-list auto-approves `gblog archive --file` for the transcript directory, and names `--no-redact` as never auto-allowed. A transcript with one NUL (a tool that printed binary output, or a prompt-injected Bot that appends `\0`) gets the effect of `--no-redact` through the auto-approved path.
- NUL is a valid UTF-8 code point, so a mostly-text file with a stray NUL could be scanned losslessly.
- UTF-16 exports (PowerShell `>`, Windows editors) are text and are never scanned.

**Reproduction:**

```bash
printf 'export DB_PASSWORD=hunter2hunter2\n\000' > /tmp/t.jsonl
gblog archive --bot ledger --kind transcript --file /tmp/t.jsonl; echo $?   # 0, one warning line
zcat "$GBLOG_ROOT"/archive/ledger/*/*.gz | grep -a hunter2                  # present
```

**Fix.** Replace the two-way decision in `archive_file` with four tiers. The first tier that applies wins.

1. **UTF-16 / UTF-32 with BOM**, strict decode succeeds: redact the text and re-encode with the same codec and BOM. No `scanned` key.
2. **Strict UTF-8 succeeds and NUL bytes are at most 0.1 % of the length**: text, as today. The NUL bytes round-trip unchanged.
3. **No BOM, even length, at least 30 % NUL at the odd (or even) offsets of the first 4 KiB**: try `utf-16-le` (`utf-16-be`) strictly, then as tier 1.
4. **Everything else:**
   - no NUL: the `surrogateescape` path, as today;
   - with NUL (true binary): never rewrite the bytes. Decode a `surrogateescape` view and run `redact()` on it only to detect.
     - If the view would change and `--no-redact` was not given, refuse: `archive: binary content holds N secret-shaped string(s); nothing stored (pass --no-redact to store it as it is)`, exit 2, before the file is written.
     - Otherwise store with the warning and `scanned: false`, as today.

The 0.1 % bound keeps NUL-padded containers (tar) out of the text path, where a rewrite would corrupt them.

**Decision for the maintainer:** tier 4 turns "store with a warning" into "refuse" for binary content that holds secret shapes. That makes `--no-redact` the only way to store such content, which is what the allow-list assumes. The alternative is to keep storing and add a count field; the bypass then stays open.

**Tests:**

- The reproduction: the stored content has `DB_PASSWORD=[REDACTED]`, the NUL is still there, `redacted` is true, no `scanned` key.
- `"token=abcdef123456\n".encode("utf-16")`: the stored bytes decode as UTF-16, hold `[REDACTED]`, and start with the BOM.
- `b"\x00\x01\x02\xff\xfe" + b"token=abcdef123456"` without `--no-redact`: exit 2, nothing under `archive/`, no record. With `--no-redact`: stored verbatim, `scanned: false`.
- `b"\x00\x01\x02\xff\xfe binary screenshot bytes"`: unchanged behaviour (warning, `scanned: false`).
- Existing tests that change:
  - `tests/test_gblog.py::test_archive_roundtrip_dedup_redaction_and_verify`, last block: that blob is valid UTF-8 with 3 NULs in 21 bytes, so it falls into the refuse tier.
  - `tests/test_cli_ux.py::test_archive_binary_marks_unscanned`, UTF-16 part: it is scanned now.
- README, `SECURITY.md` ("archive content that is not UTF-8 and holds a NUL byte") and FAQ 18 need the new wording.

---

## K12-03 (High) — Key-prefix rules redact ordinary names in every text field

**Where:** `gblog.py`, `_SECRET_PATTERNS`.

```python
(re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
(re.compile(r"\bxai-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
(re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"), "[REDACTED]"),
```

**Problem.** D6-01 exempted only the generated `path` / `sha256` of archive records (`_stored_value`). Every other text field that mentions such a name is still rewritten. On a Grok Bot computer, strings that start with `xai-` (repository names, package names, host names, Bot slugs) are common.

**Reproduction:**

| Command | Stored |
|---|---|
| Daily routine step 5: `gblog archive --bot ledger --kind transcript --file t.jsonl --detail "xai-research-assistant transcript"` | `detail: "[REDACTED] transcript"`. `target` is the base name `<date>.jsonl`, so the record no longer says whose transcript it is. |
| `gblog sync --no-push --remote https://github.com/you/xai-grokbot-activity-ledger.git` | the B:H3 control record: `origin - -> https://github.com/you/[REDACTED].git` |
| `gblog verify --all --record` after a state mismatch in chain `sk-customer-support-bot` | `detail: "state/[REDACTED].json: head does not match …"` |
| `gblog log --action delegation --target xai-research-assistant` | `target: "[REDACTED]"` |
| `capture files`, file `skills/sk-customer-support-bot/SKILL.md` | `target: "skills/[REDACTED]/SKILL.md"` |

**Fix.** A real key has a random tail; a name has words. Use a replacement function for the three rules:

```python
_WORD_SEG_RE = re.compile(r"[a-z]{1,15}|[0-9]{1,4}")

def _wordy(tail: str) -> bool:
    """customer-support-bot, sdk-python-examples, ledger-2026: lower-case words and short numbers only."""
    segs = [s for s in re.split(r"[-_]", tail) if s]
    return len(segs) >= 2 and all(_WORD_SEG_RE.fullmatch(s) for s in segs)

def _key_repl(match) -> str:
    text = match.group(0)
    return text if _wordy(text.split("-", 1)[1]) else "[REDACTED]"
```

- For the Stripe rule, exempt a tail that is `[a-z]{10,15}` (`sk_test_environment`). Real Stripe keys have 24 or more base-62 characters.
- A base-62 key of 20 characters is all lower case with probability about (26/62)^20 ≈ 3·10⁻⁸. A segment that mixes letters and digits is never "wordy", so hex-like keys stay redacted.
- Optional second step, for names with capitals or long numbers (`sk-SupportBot-v2`): in `Ledger.append`, protect registered slugs, aliases and existing chain names with placeholders around the `redact()` call. Step 1 covers the cases above without touching `redact()`'s signature.

**Tests:**

- `redact()` returns these unchanged: `xai-research-assistant transcript`, `https://github.com/you/xai-grokbot-activity-ledger.git`, `skills/sk-customer-support-bot/SKILL.md`, `sk_test_environment`.
- Still redacted: the existing vendor cases of `test_every_documented_vendor_shape`; `sk-aaaaaaaaaaaaaaaaaaaaaaaa`; `sk-proj-` + mixed tail; `xai-` + 20 lower-case hex characters with digits.
- End to end: the routine command above stores the detail unchanged; `sync --remote …/xai-grokbot-activity-ledger.git` records the full URL.
- `tests/test_deep_reviews.py::test_archive_target_is_still_redacted` still passes (it uses `token=…`).

---

## K12-04 (Medium) — Assignment rules destroy common non-secrets

**Where:** `gblog.py`, `_redact_env_names` (rule 1), rule 2, `_glued_name_repl`, the bearer rule and the unprefixed flag rule.

**Problem and reproduction.** Each line is `gblog log/note … --target/--detail '<left>'`; the right column is what is stored.

| Typed | Stored | Rule |
|---|---|---|
| `docker run -v $PWD:/workspace/app img` | `docker run -v $PWD=[REDACTED] img` | rule 1: `pwd` + `:` + 6 or more characters |
| `PWD=/workspace/project` (every `env` dump in a transcript) | `PWD=[REDACTED]` | rule 1 |
| `OLDPWD=/workspace` | `OLDPWD=[REDACTED]` | glued rule |
| `prompt_token_count=123456` | `prompt_token_count=[REDACTED]` | rule 1; the W-17 counter exemption exists only in the glued rule, for names ending in `tokens` |
| `tokens_used=123456`, `tokenCount=128000`, `max_token=100000` | `…=[REDACTED]` | glued rule / rule 1 |
| `401: token: expired` | `401: token=[REDACTED]` | rule 1 |
| `login failed: password: incorrect` | `…password=[REDACTED]` | rule 1 |
| `API uses bearer authentication` | `API uses Bearer [REDACTED]` | bearer rule |
| `psql --password -h db.internal -U app` | `psql --password [REDACTED] db.internal -U app` | flag rule: `--password` is a boolean flag in psql; the `-h` is eaten. The prefixed-flag rule has a `(?!-)` guard, this one does not. |

The mount target, usage numbers, error causes and flags are the evidence `tune` works from ("Last error: …").

**Fix.** One predicate, called from all three assignment rules before a match is replaced. `name` is the matched name, `value` the unquoted value.

```python
_COUNTER_WORDS = frozenset(("count", "counts", "limit", "limits", "usage", "used", "budget", "total", "max", "min",
                            "num", "avg", "len", "length", "size", "window", "remaining", "left", "rate", "per"))
_STATUS_WORDS = frozenset(("expired", "invalid", "missing", "required", "incorrect", "rejected", "revoked", "denied",
                           "failed", "wrong", "changed", "rotated", "unknown", "undefined", "mismatch", "always", "never"))
_NAME_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

def _not_a_secret(name: str, value: str) -> bool:
    if name in ("PWD", "OLDPWD") or (name == "pwd" and value[:1] in "/~.$"):
        return True                                   # the working directory, not MYSQL_PWD
    if value.lower() in _STATUS_WORDS:
        return True                                   # "token: expired"
    if _COUNTER_VALUE_RE.match(value):                # digits, "_", ".", "," only
        words = [w.lower() for w in _NAME_WORD_RE.findall(name)]
        return "tokens" in words or ("token" in words and any(w in _COUNTER_WORDS for w in words))
    return False
```

- Rule 1: test in `_redact_env_names` after `name_start` is known; `continue` when true.
- Rule 2 and the glued rule: replacement functions that call the predicate. `_glued_name_repl` already has this shape; the predicate subsumes its `endswith("tokens")` test.
- Still redacted, as the existing tests require: `token=123456`, `refresh_token=123456789`, `dbPassword=123456`, `tokens=abc123def456`, `max_tokens=abc123def456`, `prompt_tokens="123456"`.
- Bearer rule: replacement function that keeps the match when the candidate is a plain word (`[a-z]+` or `[A-Z][a-z]+`).
- Flag rule: separator `(?:=| +(?!-))` in place of `[ =]+`. `--password=-x` is still redacted.

Add the three word lists to `SECURITY.md`, so the exemptions are public.

**Tests:**

- The nine "Typed" strings are returned unchanged by `redact()`.
- Still redacted: `MYSQL_PWD=sup3rs3cret1`, `db_pwd: hunter2hunter2`, `Authorization: Bearer abcdef123456`, `--password hunter2hunter2`, `--token=abcdef123456`, and the six strings listed above.
- `TokenCounterTest` and the differential test of `_LinearRule` pass (update the oracle for rule 1).

---

## K12-05 (Medium) — Gaps inside shapes the documents list as covered

**Where and reproduction.** Each input is stored verbatim today.

1. **PGP private-key blocks.** `_PEM_BEGIN_RE = -----BEGIN [A-Z ]*PRIVATE KEY-----` and the pre-check `"PRIVATE KEY-----" not in text` both miss `-----BEGIN PGP PRIVATE KEY BLOCK-----`. A `gpg --export-secret-keys --armor` output in a transcript is archived whole. README and `SECURITY.md` say "PEM private-key blocks".
2. **URL userinfo with an empty part.** `://[^/\s:@]+:[^/\s@]+@` needs a non-empty user and password.
   - `redis://:p4ssw0rd9@cache:6379/0` (the usual Redis form);
   - `https://APIKEY123456:@api.example.com` (key as user, empty password);
   - `https://<52-char PAT>@dev.azure.com/org` (token as user, no colon). `_has_url_token` knows this shape, but only `sync` uses it.
3. **`Authorization:` with another scheme.** `curl -H "Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"`: no rule has a `Token <value>` form without `:` or `=`.
4. **JSON keys that continue after the keyword.** `{"password":"…","password_confirmation":"hunter2hunter2"}`: the first value is redacted, the second is not. The JSON-key rules need the key to *end* in the keyword. The form-encoded `password_confirmation=…` is redacted, so the two rule families disagree.

**Fix.**

1. `-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----` (and the same for `END`); pre-check `"PRIVATE KEY" not in text`. In `_PEM_BODY_RE` allow the armor headers `Version|Comment|Hash|Charset` beside `Proc-Type|DEK-Info`, for a block cut before its `END` line.
2. Userinfo rule (with the K12-01 character classes, written `U` = `[^/\s@"'<>\\]`):
   - `://(?:[^/\s:@"'<>\\]+:U*|:U+)@` → `://[REDACTED]@`;
   - a second rule `(?i)(https?://)[^/\s:@"'<>\\]{20,}@` → `\1[REDACTED]@`.
3. New rule: `(?i)((?:proxy-)?authorization\s*[:=]\s*(?:token|apikey|api-key|key|digest|negotiate|ntlm|sso-key|splunk)\s+)[^\s'",\\]{8,}` → `\1[REDACTED]`. The scheme list is closed on purpose: "authorization: pending approval" stays as written.
4. In both JSON-key rules and the dict-repr rule, allow a short closed suffix after the keyword: `(?:_?(?:confirmation|confirm|repeat|again|new|old|current)|[0-9]{1,2})?`. Not a free suffix: `"token_type": "bearer"` and `"total_tokens"` stay as written.

**Tests:**

- Each of the six inputs is redacted; context (`cache:6379/0`, `api.example.com`, `"user"` keys) survives.
- Unchanged: `https://example.com:8080/a@b`, `"token_type": "Bearer"`, "authorization: pending approval".
- Idempotency for all new outputs.
- The PEM timing test (`test_many_pem_begin_markers_do_not_stall_redaction`) still passes.

---

## K12-06 (Medium) — Browser capture stores full URL paths; titles cannot be switched off

**Where:** `gblog.py`, `_url_target()` and `capture_browser()`.

```python
host = parts.hostname or parts.netloc
return "%s://%s%s" % (parts.scheme, host, parts.path or "/")
```

**Problem.** The README says "It stores no query string or fragment". The path can hold the same kind of value:

- capability links: `https://docs.google.com/document/d/<44-char id>/edit`, `/reset-password/<token>`, `/invite/<code>`, share links;
- path parameters: `urlsplit` keeps `;jsessionid=5A3F…` inside `path`, so a live session id is stored;
- personal data: `/users/jane.doe@example.com/settings`.

`redact()` sees these only when they have a known shape. On a shared computer every Bot imports the same visits, so each value lands in N chains and is pushed N times. Titles (120 characters, for example mail subjects) are always stored. `SECURITY.md` lists them as "not covered", but there is no switch.

**Reproduction:** build a history fixture with `https://shop.example/cart;jsessionid=5A3F9C0D1E2B?x=1` and run `gblog capture browser --bot ops --history <file>`. The record's `target` is `https://shop.example/cart;jsessionid=5A3F9C0D1E2B`.

**Fix.**

1. `capture browser --paths full|masked|host`. The default stays `full` (no behaviour change). `masked` applies:
   - drop path parameters (everything from `;` in a segment);
   - replace a segment with `[…]` when it contains `@`, or is 20 or more characters of `[A-Za-z0-9_\-=.~%]` and is not "wordy" (reuse `_wordy()` from K12-03, so article slugs such as `how-to-reset-your-password` stay).
   
   `host` stores `scheme://host/`.
2. `capture browser --no-titles`: store no `detail`.
3. `docs/routine-weekly-tune.md` and the `bot-tune` skill pass `--paths masked`. `coverage` normalises browser targets to the host (`norm_target`), so matching does not change.
4. README wording: "no query string or fragment; the path is stored (see `--paths`)".

**Tests:**

- `_mask_path` unit cases: the Google Docs id, a `;jsessionid` segment and an e-mail segment are masked; `/a/b/how-to-reset-your-password/` and `/x/y.html` are unchanged.
- `--paths host` stores `https://shop.example/`.
- `--no-titles` stores no `detail`.
- Coverage of one self-reported visit is 100 % in all three modes.
- The default (`full`) output and records are byte-identical to today's (existing browser tests untouched).

---

## K12-07 (Medium) — New rules never reach what is already stored

**Where:** `gblog.py`, readers and renderers: `render_report()`, `render_tune()`, the `tail` and `export` branches of `_main()`; `_md()` and `_term()`.

**Problem.** Redaction happens once, in `append` and `archive_file`, with the rules of that version. 0.6.0 and 0.6.1 added about twenty shapes. A ledger written by 0.1–0.5 holds text those rules would now catch, and:

- `gblog report --since <old date>` and `gblog tune` render it again into new files under `reports/` and `tuning/`, which the next `sync` commits and pushes;
- `gblog export` prints it for whoever receives the export;
- no command tells the operator *where* such text is. "If a secret reaches the ledger: rotate first" cannot start.

**Fix.**

1. **`gblog scan [--bot B] [--since D] [--archives] [--json]`**, read-only.
   - For every readable record and top-level string field, compare `redact(value)` with `value`.
   - Print `logs/<bot>/<file>:<line>: field '<name>' holds secret-shaped text`. Never print the text.
   - With `--archives`, do the same per archived text file (`<path>: N line(s)`), through `_gunzip_sha256`-style bounded reads.
   - Exit 0 when nothing was found, 1 otherwise.
   - It never rewrites anything; what to do with a hit stays the procedure in `SECURITY.md`.
2. **Render-side redaction.** `_md()` and the `tail` line builder apply `redact()` to record text before neutralising it. Reports and tuning files are derived data, and the call is idempotent for text that was already redacted. With K12-03 and K12-04 in place this adds no new false positives.
3. **`gblog export --redact`.** Apply `_stored_value` to each top-level string on output. Without the flag `export` stays a faithful dump.

**Tests:**

- Write a raw hash-valid line whose `detail` holds `dbPassword=hunter2hunter2` (as a pre-0.6.1 gblog would have stored it).
  - `scan` exits 1, names file, line and field, and its output does not contain `hunter2`.
  - `report --date … --stdout` and `tail` do not contain it.
  - `export` contains it; `export --redact` does not.
  - `verify` is still OK.
- `scan` on a clean ledger: exit 0, empty stdout.
- `scan --json` is a list of `{file, line, field}` objects.
- `scan` creates no file (compare a tree listing before and after).

---

## K12-08 (Medium) — Archives stored without scanning are invisible downstream

**Where:** `gblog.py`, `archive` branch of `_main()`, `render_report()`, `status`, `sync()`.

```python
line = ("%s #%d archive %s sha256=%s %d bytes%s%s\n" % (…,
        " (redacted)" if rec.get("redacted") else "", " (already stored)" if rec.get("_dedup") else ""))
```

**Problem.**

- `--no-redact` prints nothing: no stderr line and no marker on the result line. `redacted` is `false`, which reads like "nothing needed redacting".
- The only trace is `scanned: false` in the raw record. `report` renders the record as ``- 09:00:02 `archive` e.txt``; `tail`, `status` and the `verify` line do not show it.
- The next `sync` pushes the file. The operator who reads the daily summary cannot see that unscanned content left the computer.

**Fix** (all additive):

1. Result line: append ` (not scanned)` when the record has `scanned: false`, in the same position as ` (redacted)`. Document it in `docs/agent-integration.md`, section 3.3.
2. `report` and `tail`: append `(not scanned)` to an `archive` line whose record has `scanned: false`.
3. `status --json` rows: `unscanned_archives`, counted over the 30-day window `status` already reads.
4. `sync`: when records of type `archive` with `scanned: false` are newer than the chain's `last_sync.ts`, print `notice: this commit adds N archived file(s) stored without secret scanning (<chain>: …)` on stderr. Step 7 of the daily routine prompt reports that line.

**Decision for the maintainer:** item 1 changes one stdout line (a suffix, like the two existing ones). If stdout must stay frozen, use a stderr `notice:` line instead.

**Tests:**

- `archive --no-redact`: the output ends with ` (not scanned)`.
- The report line carries the marker; a scanned archive's line is unchanged.
- `status --json` counts 1.
- `sync --no-push` prints the notice once; a second `sync` does not.

---

## K12-09 (Low) — Identifiers are redacted silently instead of refused

**Where:** `gblog.py`, `_stored_value()` (every top-level string), `seal` and `task-start` branches of `_main()`.

**Problem.**

- **`--jay-approval-id`** is stored through `redact()`. An id with a secret shape (a signed approval as a JWT, a permalink with `?code=…`, anything starting with `sk-`) is stored as `[REDACTED]` on the `break_ack` and on the witness note. `seal` can run once and cannot be corrected. `SECURITY.md` says "compare the stored id with the operator's own record of it"; that comparison is then impossible. The CLI prints the id as typed, so nothing shows the loss.
- **`--task-id` / `$GBLOG_TASK`** are redacted too. `_task_status` compensates (`stored = redact(task_id) or task_id`), but two different ids that both redact to `[REDACTED]` become one task in `report` and `status`.
- **`--target` / `--detail` / `--task`:** when redaction changes a field, the caller is not told. A Bot cannot notice a false positive and restate the fact.

**Fix.**

1. `seal`: before `seal_break`, `if redact(jay_id) != jay_id:` refuse with `seal: jay_approval_id would be stored as '<redacted form>'; pass an id without secret-shaped text`, exit 2, nothing written. The same check belongs in `Ledger.seal_break` for library callers.
2. `task-start --task-id`, and `--task-id` / `$GBLOG_TASK` on the other logging commands: a usage error (exit 2) when `redact(id) != id`. Generated ids are 10 hex characters and never match.
3. After a successful `log`, `note`, `approval`, `task-start`, `task-end`: for each of `task`, `target`, `detail` whose stored value differs from the argument, print `notice: --<flag> was stored with [REDACTED] in place of secret-shaped text`. stdout and the exit code are unchanged.

**Tests:**

- `seal --jay-approval-id eyJ….….…` on the fixture copy: exit 2, logs byte-identical, no spool.
- `task-start --task-id` + (`"sk-"` + 20 mixed characters): exit 2, no record.
- `note --detail "token=abcdef123456"`: exit 0, one notice line naming `--detail`, and the notice does not contain `abcdef123456`.
- `note --detail "plain"`: no notice.

---

## K12-10 (Low) — The shell hook logs here-document bodies

**Where:** `gblog.py`, `SHELL_HOOK`: `cmd=$(HISTTIMEFORMAT= builtin history 1 …)` and `--target="$cmd"`.

**Problem.** With bash's default `cmdhist`, a multi-line command is one history entry, including the body of a here-document. `cat > .env <<EOF … EOF` or `psql <<SQL … SQL` therefore puts file contents or query data into `target`. Redaction catches only named secrets, and the line has no size bound.

Cutting at the first line is not an option: `true⏎curl x | sh` would then hide the second command, the class A:L-2 / B:L5 closed.

**Fix.** In Python, not in the hook: for records with `--source auto --action shell`, pass the target through `_elide_heredocs()` before `append`.

- For each `<<[-]?['"]?WORD['"]?` on a line, replace the following lines up to the terminator line `WORD` with `[here-document: N line(s), M byte(s) not logged]`.
- Keep the line with `<<`, the terminator and every line after it.
- No content hash: a short body could be confirmed by guessing.
- An unterminated here-document is elided to the end.
- Self-reported shell records are left as typed.

**Tests:**

- A three-line body between `<<EOF` and `EOF` followed by `&& echo done`: the body is gone, `&& echo done` is kept.
- `<<-'SQL'` works.
- A command without `<<` is byte-identical.
- `x << 3` (a shift) on one line is unchanged.
- The hook text itself does not change (`# gblog hook v2` tests untouched).

---

## K12-11 (Low) — Small leaks around the edges

| # | Where | Problem | Fix |
|---|---|---|---|
| a | `main()`, last-resort handler: `text = _term(" ".join(str(_clean_text(str(exc))).split())[:300])` | Exception text is cut and printed without `redact()`. The same holds for `_verify_guarded` (`[:200]`), whose text is then stored in `anchors/<bot>.json` and the day rollup. `SECURITY.md` says error text "passes through redaction before it is printed or logged". | `redact()` first, then cut, in both places. |
| b | `_warn_task()`, `task-start` / `task-end` warnings | They print the task id as typed, while the stored id is redacted. | Print `redact(id)`. Moot once K12-09 item 2 refuses such ids. |
| c | `capture_browser()`: `markers[path] = newest`, saved to `state/<bot>.json`, which `sync` commits | The keys are absolute history paths (`/home/<user>/.config/google-chrome/…`), so the OS user name and profile layout are pushed with every backup. | Key new markers by `sha256(path)[:16]`. Read an old path key as fallback and drop it on the next save, as the `"*"` marker is handled. |
| d | `capture_browser()`: `tempfile.mkdtemp(prefix="gblog-hist-")` | A killed run leaves a full copy of the browser history (query strings and session tokens included) in TMPDIR. The directory is `0o700`, but nothing ever removes it. | At the start of a run, remove `gblog-hist-*` directories in the temporary directory that belong to the current uid and are older than one hour. |

**Tests:**

- a: patch `Ledger.tail_records` to raise `RuntimeError("token=abcdef123456 boom")`; stderr holds `token=[REDACTED]`.
- c: after a capture, `state/<bot>.json` contains no `/`-separated path; a state file with an old path key imports nothing twice.
- d: a stale `gblog-hist-x` directory with an old mtime is gone after a run; a fresh one is kept.

---

## K12-12 (Low, design) — No PII handling

**Where:** `_stored_value()`; the `message` row of `skills/activity-log/SKILL.md`.

**Problem.** `redact()` targets credentials only. Recipients (`--action message --target jane.doe@example.com`), page titles ("Inbox (3) – jane.doe@example.com") and file names are stored and pushed as typed. The skill leaves it to the Bot ("names or roles … if the user prefers"), and the code has no switch.

**Fix (opt-in, off by default, so nothing changes for existing ledgers):**

- Environment variable `GBLOG_PII=mask`, read by `_stored_value` and the browser title path. In mask mode:
  - an e-mail address becomes `[EMAIL]@<domain>`; the domain stays, because `coverage` and `tune` group by target;
  - an E.164 phone number (`\+[0-9]{8,15}`) becomes `[PHONE]`.
- Archives are not touched in this step: masking addresses in a transcript changes evidence, which needs the operator's decision.
- No plain hash of the address: it can be reversed with a dictionary.
- Document in `SECURITY.md` that the mode is set in the operator's environment, not in a file under the ledger root. The adversary can write there, and a rule file could then be used to blank future records.

**Tests:**

- With the variable set: `--target jane.doe@example.com` is stored as `[EMAIL]@example.com`; two messages to the same domain still group in `tune`.
- Without it: byte-identical to today.

---

## Checked, no finding

- **Spool and alert payloads:** `_refuse_tail` stores the event through `_stored_value`; `spool replay` runs it through `append` again.
- **`sync`:** the commit message, refusal texts, push errors (redacted before the cut) and the `visibilities` list all pass `redact()` or `_safe_url()`. The password of a `--remote` URL is never echoed.
- **Browser titles:** redacted before the 120-character cut; the second pass in `append` is harmless.
- **`--json` outputs** (`verify`, `status`, `anchor`, `tune`, `coverage`) and the `GBLOG_ERRORS=json` line: they carry only text that is already in the corresponding text output; `coverage --json` omits `unmatched`.
- **Archive record fields under `--no-redact`:** `--name` and `--detail` are still redacted; only the content is exempt, as documented.
- **Git index:** the file-capture snapshot (`state/capture-files-*.json`, a full file listing) is kept out by `.gitignore`, `_sync_evidence` and the `git rm --cached` step.

## Suggested order

1. K12-01, K12-03, K12-04: regex changes in one place, with the largest effect on evidence quality.
2. K12-02 and K12-08: both need a maintainer decision before code.
3. K12-05, K12-07, K12-06.
4. The rest.

K12-07's render-side redaction should land after K12-03 and K12-04, so that it does not spread today's false positives into every report.