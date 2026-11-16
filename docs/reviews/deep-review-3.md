# Deep review 3 — sync, git handling, remote visibility, redaction of what reaches git

Scope: `sync()`, `_git`, `_git_env`, `_sync_evidence`, `_sync_ignored`, `_sync_drift`, `_write_sync_cache`, `_remote_host_path`, `github_remote`, `_ssh_alias_target`, `remote_visibility`, `_safe_url`, `_has_url_password`, the `sync` CLI branch, the parts of `redact()` that decide what text reaches a commit, and the docs that describe them.

Method: static read only. Nothing was executed. Reproductions are command sequences for temporary directories, with no network. Where a finding depends on what git, ssh or libcurl do with a string, it is from my reading of their sources (`connect.c`, `transport.c`, `builtin/add.c`, `builtin/remote.c`) and is marked "confirm".

## Summary

| ID | Sev | Finding |
|---|---|---|
| D3-01 | High | GitHub URL forms that git accepts are classified "another host": bracketed scp-like hosts, `<transport>::<address>`, percent-encoded or IDN hosts, unparsable URLs |
| D3-02 | High | `…/repo.wiki.git` answers 404, is taken as "private", and is pushed to a public wiki |
| D3-03 | Med-High | With no repo-level `origin`, sync only warns and then lets `git push origin` resolve the name elsewhere |
| D3-04 | Medium | An ignore rule that names a top-level ledger directory makes `git add -A` exit 1, so sync exits 2 and the force-add is never reached |
| D3-05 | Medium | The sync cache lets a same-size rewrite with restored mtime pass the committed-content check; DESIGN §13 and the FAQ say there is no cache |
| D3-06 | Medium | `text=True` decoding: one undecodable byte in git output ends in "internal error" after a successful push |
| D3-07 | Medium | `commit.gpgSign` / `push.gpgSign` in the user's git config break or hang sync |
| D3-08 | Medium | A symlinked `archive/` (documented as supported) makes every sync fail with a misleading message |
| D3-09 | Low-Med | `GIT_DIR`, `GIT_INDEX_FILE` and `GIT_CONFIG_COUNT` are inherited: wrong repository, or redirected push without a record |
| D3-10 | Low-Med | `origin` is changed before the record can be written (`--bot aliases`, any append error) |
| D3-11 | Low | `set-url` runs even when nothing changes; multi-URL origins and `pushurl` are mishandled |
| D3-12 | Low | A token used as URL user name: no warning, and git error text is only passed through `redact()` |
| D3-13 | Low | sync writes a genesis state for an emptied chain directory; the next `anchor` then overwrites the surviving anchor |
| D3-14 | Low | `NAME=value` redaction stops at `&` or a quote: `DB_PASSWORD=Sup3r&Secret!` is stored as written |
| D3-15 | Low | Documentation drift in this area |
| D3-16 | Low | Small robustness items |

---

## Findings

### D3-01 (High) — URL forms git pushes to GitHub that the classifier calls "another host"

**Where:** `_remote_host_path`, `github_remote`.

```python
    if "://" in u:
        ...
        try:
            parts = urlsplit(u)
            host = parts.hostname
        except ValueError:
            return None, None, ""            # unparsable -> "no host" -> warn and push
    head, sep, tail = u.partition(":")
    if sep and "/" not in head:
        return (head.rpartition("@")[2].lower().rstrip(".") or None), tail, "scp"
```

This is the same class as A:H-2 / B:H2. The fix parsed `scheme://host` and `user@host:path`, but git accepts more forms:

| Remote string | What git does (confirm) | gblog today |
|---|---|---|
| `[github.com:22]:o/r.git`, `git@[github.com]:o/r.git` | ssh to github.com; `host_end()` / `get_host_and_port()` unwrap the brackets (t5601 "bracketed hostnames are still ssh") | host is `[github.com`, so "another host" |
| `https::https://github.com/o/r.git` (also `http::`, `ftp::`) | `transport_get()` takes the `<transport>::` prefix as a remote helper and `remove_ext_force()` hands it the address, so `git remote-https` pushes to GitHub | `urlsplit` finds no netloc, so "another host" |
| `ssh://git@github%2Ecom/o/r.git` | `parse_connect_url()` calls `url_decode()` before splitting, so it connects to github.com | host is `github%2ecom`, so "another host" |
| `https://github%2Ecom/o/r`, `https://ｇｉｔｈｕｂ.com/o/r`, `https://github。com/o/r` | depends on libcurl (percent-decoding of host names; IDNA mapping with libidn2) | "another host" |
| `ssh://git@[github.com]/o/r.git` on Python ≥ 3.11.4 | brackets unwrapped, github.com | `urlsplit` raises `ValueError`, so "no host" (fail-open) |
| `ssh://git@evil.example%2F@github.com/o/r.git` | decoded first, so the host is `evil.example` | host `github.com`; `o/r` is looked up and can be reported "private" |

In the first five rows the ledger goes to a public GitHub repository with only `warning: could not confirm the remote is private (not a github.com remote; visibility unknown)`. SECURITY.md lists exactly this as a vulnerability.

**Reproduction (no network):**

```bash
python3 - <<'PY'
import gblog
for u in ("[github.com:22]:octocat/Hello-World.git", "git@[github.com]:octocat/Hello-World.git",
          "https::https://github.com/octocat/Hello-World.git",
          "ssh://git@github%2Ecom/octocat/Hello-World.git"):
    print(gblog.github_remote(u), gblog.remote_visibility(u)["reason"])
# each: (False, None, None) not a github.com remote; visibility unknown
PY
# what git does with the same strings; ssh is replaced by a stub that prints its arguments
export GIT_SSH_VARIANT=ssh GIT_SSH_COMMAND='sh -c "echo SSH \"\$@\" >&2; exit 255" stub'
git ls-remote '[github.com:22]:octocat/Hello-World.git'          # SSH ... -p 22 github.com git-upload-pack ...
git ls-remote 'ssh://git@github%2Ecom/octocat/Hello-World.git'   # SSH ... git@github.com git-upload-pack ...
# helper form: a fake helper shows which address the helper receives
mkdir -p "$T/bin"; printf '#!/bin/sh\necho "helper got: $2" >&2\n' > "$T/bin/git-remote-demo"; chmod +x "$T/bin/git-remote-demo"
PATH="$T/bin:$PATH" git ls-remote 'demo::https://github.com/o/r.git'   # helper got: https://github.com/o/r.git
```

**Fix.** Parse the way git does, and never map to a repository a host that had to be normalised.

```python
_HELPER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9+.-]*::")
_AUTH_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)", re.S)

def _host_of(hostport):                       # [user@]host[:port], [host], [host:port], [v6]
    hostport = hostport.rpartition("@")[2]
    if hostport.startswith("[") and "]" in hostport:
        inner = hostport[1:hostport.index("]")]
        return inner.partition(":")[0] if inner.count(":") == 1 else inner
    return hostport.partition(":")[0]

def _norm_host(host):
    from urllib.parse import unquote
    host = unquote(host)
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")   # NFKC, fullwidth letters, the four IDNA dots
        except UnicodeError:
            pass
    return host.lower().rstrip(".") or None
```

Rules for `_remote_host_path` (same return shape; `kind` gets the prefix `odd:` when a rule marked the URL):

1. Strip a `<transport>::` prefix and mark the URL odd.
2. For `scheme://` URLs, use `_AUTH_RE` instead of `urlsplit().hostname`, so no `ValueError` path remains. When the scheme is not http(s)/ftp(s) and the URL contains `%`, `unquote()` the whole URL before splitting (as `connect.c` does) and mark it odd.
3. Take the host with `_host_of` and `_norm_host`. Mark it odd when normalisation changed it, or when the authority contains `[`, `%`, `\` or a non-ASCII character.
4. For scp-like strings, handle `@[` / a leading `[` up to `]` followed by `:` first, as `host_end()` does, and mark it odd. Otherwise parse as today.
5. A string that still cannot be parsed and contains `::` or `://` gets kind `invalid`.

Rules for `github_remote`:

- kind `invalid`: return `(True, None, None)` (fail closed).
- an `odd:` kind with a GitHub host: return `(True, None, None)`. The URL is never mapped to owner/repo, so sync exits 3 with "cannot be mapped to a repository" and the operator rewrites it in plain form.
- an `odd:` kind that came from a helper prefix, where the rest of the string mentions a GitHub host name anywhere: return `(True, None, None)`.
- otherwise as today.

In `_ssh_alias_target`, compare `kind` after removing the `odd:` prefix, and skip the lookup for odd kinds.

---

### D3-02 (High) — a wiki remote is "confirmed private" by a 404

**Where:** `github_remote`, `remote_visibility`.

```python
_GITHUB_PATH_RE = re.compile(r"/*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/*\Z")
...
        if exc.code == 404:
            return {"public": False, "reason": "api.github.com 404 (private or nonexistent)"}
```

`https://github.com/o/r.wiki.git` maps to repo `r.wiki`. `GET /repos/o/r.wiki` is 404 for every repository (confirm), and `gh api` fails the same way and falls through. The answer is `public: False` and the push proceeds, but the wiki of a public repository is world-readable (`git clone https://github.com/o/r.wiki.git`). Gists were made fail-closed; wikis were missed.

**Reproduction (offline):**

```python
import gblog, urllib.error
from unittest import mock
print(gblog.github_remote("https://github.com/octocat/Hello-World.wiki.git"))  # (True, 'octocat', 'Hello-World.wiki')
err = urllib.error.HTTPError("u", 404, "Not Found", None, None)                # what the API answers for <repo>.wiki
with mock.patch.object(gblog.subprocess, "run", side_effect=OSError), \
     mock.patch("urllib.request.urlopen", side_effect=err):
    print(gblog.remote_visibility("git@github.com:octocat/Hello-World.wiki.git"))  # {'public': False, ...}
```

**Fix** in `github_remote`: a wiki is as public as its repository.

```python
        if m and not ({m.group(1), m.group(2)} & {".", ".."}):
            repo = m.group(2)
            if repo.lower().endswith(".wiki"):
                repo = repo[:-5]
            if repo:
                return True, m.group(1), repo
```

Add one row to the SECURITY.md table: "wiki of a repository: checked as that repository".

---

### D3-03 (Med-High) — no repo-level `origin`: a warning, then an unchecked push

**Where:** `sync()`.

```python
        urls = [u.strip() for u in _git(root, "remote", "get-url", "--push", "--all", "origin", check=False).stdout.splitlines() if u.strip()]
        ...
        elif not checks:
            sys.stderr.write("warning: could not confirm the remote is private (no origin configured)\n")
    ...
                _git(root, "push", "--no-verify", "-u", "origin", "HEAD:%s" % cur_branch)
```

`git remote get-url` only accepts a remote configured in the repository (`remote_is_configured(remote, 1)`, git ≥ 2.26; confirm). `git push origin` resolves the name more widely:

- global or system config, or config injected through `GIT_CONFIG_COUNT`;
- the legacy files `.git/remotes/origin` and `.git/branches/origin`;
- a directory named `origin` under the root, taken as a path.

In each case `checks` is empty, `remote_visibility` is never called, and the push runs. With a public GitHub URL in a global `[remote "origin"]`, that is a push to a public repository without an override. Any error from `get-url` (git older than 2.7, a broken repository) has the same fail-open effect.

`current` is read with `git config --get-all remote.origin.url` (all scopes), which disagrees with `get-url` and with `git remote set-url`. With a global origin, `--remote X --replace-remote` ends in `git failed: No such remote 'origin'`.

**Reproduction:**

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger
gblog note --bot ops --detail x
git init -q --bare "$GBLOG_ROOT/origin"        # or: .git/remotes/origin, or [remote "origin"] in $GIT_CONFIG_GLOBAL
gblog sync; echo $?
# stderr: warning: could not confirm the remote is private (no origin configured)
# stdout: commit <sha> pushed        exit 0
git -C "$GBLOG_ROOT" remote                    # empty: the ledger repository has no origin and no "git remote" record
```

**Fix:**

```python
        if not urls:
            result["error"] = "no origin configured in the ledger repository; set one with `gblog sync --remote <url>`"
            push = False
```

This replaces the `elif not checks:` warning. The exit code stays 1 and the output stays `commit <sha> (not pushed)`, which is what FAQ 26 already describes. In the `--remote` block, read `current` with `config --local --get-all remote.origin.url`, and choose `remote add` or `set-url` from `current` (see D3-11).

---

### D3-04 (Medium) — an ignore rule on a top-level directory aborts sync before the force-add

**Where:** `sync()`.

```python
    _git(root, "add", "-A", *add_paths)          # check=True
    ...
    hidden = _sync_ignored(root)                 # never reached
```

When a pathspec that was named on the command line is itself ignored, `git add` stages the rest, prints `The following paths are ignored by one of your .gitignore files:` and exits 1. `_git` raises, and the CLI prints `git failed: …` with exit 2. Nothing is committed, and every retry appends another `sync pending` record.

The nested-`.gitignore` case that B:M4 fixed does not trigger this, because there `logs` itself is not ignored. A line `logs/`, `state/`, `archive/` or `spool/` in `.git/info/exclude`, in the root `.gitignore`, or in a global `core.excludesFile` does. The README says sync "commits gblog's files even when a git ignore rule would hide them".

**Reproduction:**

```bash
export GBLOG_ROOT=$(mktemp -d)/ledger
gblog note --bot ops --detail x && gblog sync --no-push
echo 'logs/' >> "$GBLOG_ROOT/.git/info/exclude"
gblog note --bot ops --detail y; gblog sync --no-push; echo $?
# git failed: The following paths are ignored by one of your .gitignore files:
# logs
# hint: Use -f if you really want to add them. ...          -> 2
```

**Fix.** Exit status 1 from the broad add is the "ignored" case; fatal errors are 128.

```python
    added = _git(root, "add", "-A", *add_paths, check=False)
    if added.returncode not in (0, 1):
        raise subprocess.CalledProcessError(added.returncode, added.args, added.stdout, added.stderr)
```

Tracked files under the ignored directory are still updated by `-A`. New ones are listed by `_sync_ignored` (`ls-files --others --ignored` recurses into ignored directories) and force-added. `_sync_drift` remains the backstop.

---

### D3-05 (Medium) — the sync cache re-admits what the committed-content check exists to catch

**Where:** `_sync_drift`, `_write_sync_cache`, `sync()`.

```python
                entry = {"blob": sha, "size": now_size, "mtime_ns": st.st_mtime_ns}
                ok = cache.get(rel) == entry or _git_blob_id(full, size, len(sha)) == sha
...
    drift = _sync_drift(root, checked, os.path.join(root, ".git", _SYNC_CACHE))
```

DESIGN §13 says: "`sync` re-hashes the evidence files on every run. A cache of 'already verified' files would have to trust what the committed-content check exists to distrust." FAQ 35 says the same. The code has the cache, and no test covers it.

The check exists for "an index flag"; that is what the docs say. With an index flag plus a restored mtime, the cache answers for the file: the blob id, size and mtime are all unchanged, so nothing is hashed. With a remote configured, sync then prints `pushed` while a file under `logs/` differs in the pushed commit, which SECURITY.md lists as a vulnerability. The remote keeps the older bytes, so the loss is the on-box alarm.

**Reproduction:**

```bash
export GBLOG_ROOT=$(mktemp -d)/ledger
gblog note --bot ops --detail "original text" --ts 2024-06-01T10:00:00Z
gblog sync --no-push                               # the cache is written
f=$GBLOG_ROOT/logs/ops/2024-06-01.jsonl
git -C "$GBLOG_ROOT" update-index --assume-unchanged logs/ops/2024-06-01.jsonl
python3 - "$f" <<'PY'
import os, sys
p = sys.argv[1]; st = os.stat(p)
data = open(p, "rb").read().replace(b"original text", b"ORIGINAL TEXT")
open(p, "wb").write(data); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
PY
gblog sync --no-push; echo $?    # 0. Expected: 1, "committed content differs from disk for logs/ops/2024-06-01.jsonl"
```

Without the `os.utime` line, or without the cache, the drift is reported.

**Fix (recommended; removes about 35 lines):** delete `_SYNC_CACHE`, `_write_sync_cache` and the `cache_path` parameter, and call `_sync_drift(root, checked)`. Output is unchanged, and DESIGN §13 and FAQ 35 become true.

If the benchmark shows the re-hash is too slow, keep the cache but add `st.st_ctime_ns` and `st.st_ino` to the entry (`os.utime` cannot restore either), correct both documents, and add the two cache tests that the first round-3 audit named.

---

### D3-06 (Medium) — strict decoding of git output

**Where:** `_git`.

```python
    return subprocess.run([...], check=check, text=True, capture_output=True, env=_git_env())
```

`text=True` decodes with the locale encoding and `errors="strict"`. One undecodable byte raises `UnicodeDecodeError` out of `subprocess.run`. Sources of such bytes are a `remote:` line from a server hook, an error message that quotes a path, and `status --porcelain` when `core.quotePath=false`. The exception is not a `CalledProcessError`, so `main()` prints `error: internal error (UnicodeDecodeError …)` with exit 1. When this happens in the push call, the push has already succeeded: `pushed` is never set and no `last_sync` is stamped.

**Reproduction:**

```bash
T=$(mktemp -d); git init -q --bare "$T/o.git"
printf '#!/bin/sh\nprintf "\\377 caf\\351\\n"\n' > "$T/o.git/hooks/post-receive"; chmod +x "$T/o.git/hooks/post-receive"
export GBLOG_ROOT=$T/ledger; gblog note --bot ops --detail x
gblog sync --remote "$T/o.git"; echo $?        # error: internal error (UnicodeDecodeError: ...) -> 1
git --git-dir "$T/o.git" rev-list --all --count   # 1: the push happened
gblog status                                       # last sync: never
```

The hooks of the receiving repository do run. `-c core.hooksPath` travels in `GIT_CONFIG_PARAMETERS`, which git clears for the local receive-pack (confirm).

**Fix:** in `_git`, replace `text=True` with `encoding="utf-8", errors="replace"`. Do the same in the `gh` and `ssh -G` calls. `redact()` and the "nothing to commit" test work on the replaced text as before.

---

### D3-07 (Medium) — signing configuration breaks or hangs sync

**Where:** `sync()`.

```python
        _git(root, "-c", "user.name=gblog", "-c", "user.email=gblog@localhost", "commit", "-q", "--no-verify", "-m", commit_msg)
        ...
                _git(root, "push", "--no-verify", "-u", "origin", "HEAD:%s" % cur_branch)
```

With `commit.gpgSign=true` in the user's global config, git tries to sign as `gblog <gblog@localhost>`. It either fails (`gpg failed to sign the data`, exit 2 on every run with a new `sync pending` record each time) or waits for a pinentry inside a non-interactive routine. `push.gpgSign=true` fails the push against servers without signed-push support.

**Reproduction:**

```bash
T=$(mktemp -d); printf '[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n' > "$T/gitconfig"
export GBLOG_ROOT=$T/ledger GIT_CONFIG_GLOBAL=$T/gitconfig
gblog note --bot ops --detail x; gblog sync --no-push; echo $?
# git failed: error: gpg failed to sign the data ...  -> 2
```

**Fix:** add `--no-gpg-sign` to the commit and `--no-signed` to the push. gblog commits are bookkeeping, and records are unsigned by design.

---

### D3-08 (Medium) — a symlinked `archive/` makes every sync fail

**Where:** `_sync_evidence` (`take()` uses `os.lstat` on the final component only), `_sync_drift`.

DESIGN §12 and SECURITY.md say a ledger sub-directory that is a symlink "is still followed (some setups keep `archive/` on another disk)". git does not follow it: `git add -A archive` commits the link as a blob. `_sync_evidence` does follow it and puts every archived file into `checked`. `_sync_drift` finds none of them in `HEAD`. So sync always ends in `committed content differs from disk for archive/… (git ignore rule, filter or index flag); not pushed` with exit 1. The logs are not pushed either, and the message names causes that do not apply. 0.6.0 pushed without the archives and said nothing. The change is not listed under "Behaviour changes".

**Reproduction:**

```bash
T=$(mktemp -d); export GBLOG_ROOT=$T/ledger; gblog init
mv "$GBLOG_ROOT/archive" "$T/bigdisk" && ln -s "$T/bigdisk" "$GBLOG_ROOT/archive"
printf 'evidence\n' | gblog archive --bot ops --stdin --name e
gblog verify --all             # OK ... (1 archived file(s) intact)
gblog sync --no-push; echo $?  # 1, "committed content differs from disk for archive/ops/<day>/<sha>.gz ..."
```

**Fix.** Keep the fail-closed result, because the backup would lack evidence, but say what is wrong and document it.

- In `_sync_evidence`, collect directory components that are symbolic links: the five top-level directories, `logs/<chain>` and `archive/<bot>`. Return them as a third value, and leave files behind them out of `checked`.
- In `sync()`, when that list is not empty, skip the push with: `result["error"] = "%s is a symbolic link: git stores the link, not the files behind it, so they cannot be backed up; not pushed" % linked[0]`.
- Add the case to CHANGELOG "Behaviour changes", DESIGN §12 and README "Upgrading". The workaround is a mount instead of a link.

If the product decision is to push logs anyway, the same list gives a `warning:` line instead. That needs an explicit note in SECURITY.md that archives behind a link are not in the backup.

---

### D3-09 (Low-Med) — git's repository and config variables are inherited

**Where:** `_git_env`.

```python
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = "C"
```

- With an absolute `GIT_DIR` or `GIT_INDEX_FILE` in the environment (set by a hook, `rebase --exec`, or an IDE), `git -C <root>` works on another repository. sync stages the ledger there, commits whatever that index already held, and pushes that repository's `origin`.
- `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl GIT_CONFIG_VALUE_0=<url> gblog sync` redirects the push. `url.<x>.insteadOf` does the same. No `--replace-remote` is needed and no `git remote` record is written. DESIGN §1 names environment variables as adversary-controlled and already hardens `GH_HOST`.

**Reproduction:**

```bash
T=$(mktemp -d); git init -q "$T/proj"; echo wip > "$T/proj/wip.txt"; git -C "$T/proj" add wip.txt
export GBLOG_ROOT=$T/ledger; gblog note --bot ops --detail x
GIT_DIR=$T/proj/.git gblog sync --no-push
git -C "$T/proj" show --stat --format=%an HEAD    # author gblog; wip.txt and logs/ops/... in one commit
test -d "$GBLOG_ROOT/.git" || echo "the ledger has no repository of its own"
```

**Fix:**

```python
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")

def _git_env():
    env = {k: v for k, v in os.environ.items()
           if k not in _GIT_ENV_DROP and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env["LC_ALL"] = env["LANGUAGE"] = "C"
    return env
```

`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`, `GIT_ALLOW_PROTOCOL` and `GIT_TERMINAL_PROMPT` stay, because the tests rely on them. Note the change in the CHANGELOG: a ledger whose git directory is deliberately kept elsewhere through `GIT_DIR` stops working. File-based redirection (`HOME`, `GIT_CONFIG_GLOBAL=<file>`) remains possible; SECURITY.md should say so in one line.

---

### D3-10 (Low-Med) — `origin` is changed before the change can be recorded

**Where:** `sync()`: `set-url` / `remote add`, then `_bookkeep(...)`, which catches only `TailMismatchError`.

**Reproduction:**

```bash
export GBLOG_ROOT=$(mktemp -d)/ledger; gblog init
gblog sync --no-push --bot aliases --remote /tmp/elsewhere.git; echo $?   # error: bot name 'aliases' is reserved ... -> 2
git -C "$GBLOG_ROOT" remote get-url origin                                # /tmp/elsewhere.git
gblog export | grep -c '"git remote"'                                     # 0
```

The exit 2 looks like a usage error that "wrote nothing", but `origin` is now set. Later plain `gblog sync` runs push there and never write the record. Any other append failure (`ENOSPC`, a symlinked day file) has the same effect, which contradicts "every change of `origin` is written to the ledger".

**Fix:**

- At the top of `sync()`: `bot = safe_name(bot); ledger._check_reserved(bot)`, and `remote = remote.strip() if remote else remote`.
- Around the change:

```python
        if remote not in current:
            _set_origin(root, current, remote)                 # see D3-11
            try:
                _bookkeep({... "target": "git remote" ...})
            except BaseException:
                _set_origin(root, [remote], current[0] if current else None)   # put it back, or remove origin
                raise
```

---

### D3-11 (Low) — `set-url` semantics: unchanged URL, several URLs, `pushurl`

**Where:** `sync()`.

```python
        if "origin" in remotes:
            _git(root, "remote", "set-url", "origin", remote)      # also when remote is already in current
```

- With an origin that has two `url` values, `gblog sync --remote <one of them>` is documented as a no-op. It still runs `set-url`, which git refuses for a multi-valued key (`cannot overwrite multiple values`; confirm), giving exit 2. If a git version accepts it, the other URL is dropped without a record.
- `--replace-remote` changes `url` only. An existing `remote.origin.pushurl` keeps receiving the pushes, while the ledger records `origin A -> B`.

**Reproduction:**

```bash
git -C "$GBLOG_ROOT" remote add origin "$T/a.git"; git -C "$GBLOG_ROOT" remote set-url --add origin "$T/b.git"
gblog sync --no-push --remote "$T/b.git"; echo $?        # 2 "git failed: ...", expected 0
git -C "$GBLOG_ROOT" config remote.origin.pushurl "$T/old.git"
gblog sync --remote "$T/new.git" --replace-remote        # record says a.git -> new.git; the push goes to old.git
```

**Fix.** One helper, called only when `remote not in current`:

```python
def _set_origin(root, current, url):
    if url is None:
        _git(root, "remote", "remove", "origin")
    elif current:
        _git(root, "config", "--local", "--replace-all", "remote.origin.url", url)
        _git(root, "config", "--local", "--unset-all", "remote.origin.pushurl", check=False)
    else:
        _git(root, "remote", "add", "origin", url)
```

This also removes the `git remote` call and the `remotes` variable.

---

### D3-12 (Low) — credentials without a password part

**Where:** `_has_url_password`, the CLI `git failed:` line, the push-error line.

```python
        return (parts.scheme or "").lower() in ("http", "https") and bool(parts.username) and bool(parts.password)
...
            sys.stderr.write("git failed: %s\n" % redact((exc.stderr or exc.stdout or "").strip()))
...
                result["error"] = (redact((exc.stderr or exc.stdout or "").strip()) or "")[-400:]
```

- `https://<token>@host/repo.git` is the usual way to embed a PAT. It gets no warning, although `_safe_url` treats any http(s) userinfo as secret.
- Git error text goes through `redact()` only. A token that has no known shape and is used as the user name survives in `git failed: …` and `push error: …`, which the routine posts in the conversation. Older git versions echo the URL with userinfo; confirm for current ones.

**Reproduction:**

```python
import gblog
print(gblog._has_url_password("https://Zq7tokenWithNoKnownShape99@dev.example/r.git"))                    # False
print(gblog.redact("fatal: unable to access 'https://Zq7tokenWithNoKnownShape99@dev.example/r.git/': 403"))  # unchanged
print(gblog._safe_url("fatal: unable to access 'https://Zq7tokenWithNoKnownShape99@dev.example/r.git/': 403"))  # scrubbed
```

**Fix:**

- `_has_url_password` returns true when a password is present, or when the user name is 20 characters or longer, or when `redact(username) != username`. Change the warning text to "contains credentials".
- Use `_safe_url(text)` instead of `redact(text)` in both error paths. It already works on free text, and the push path stays redact-then-cut.

---

### D3-13 (Low) — sync turns an emptied chain into a "genesis" chain; `anchor` then overwrites its anchor

**Where:** end of `sync()`.

```python
    for b in ledger.valid_chains():
        ledger.update_state(b, {"last_sync": dict(stamp)})
```

For a chain directory with no log file and no state file, `update_state` writes `{"seq": 0, "head": "genesis", "last_sync": …}`. `Ledger._anchor` then no longer takes its "keep the earlier anchor" branch (the A:H-1 fix). It writes a FAIL entry with `records: 0` and no heads over `anchors/<bot>.json`. The history file and the day rollup still testify, so `verify` keeps failing; the per-chain anchor is lost.

**Reproduction:**

```bash
export GBLOG_ROOT=$(mktemp -d)/ledger
gblog note --bot ops --detail one; gblog anchor
rm "$GBLOG_ROOT"/logs/ops/*.jsonl "$GBLOG_ROOT/state/ops.json"
gblog anchor; grep -c '"records": 1' "$GBLOG_ROOT/anchors/ops.json"    # 1: kept
gblog sync --no-push; gblog anchor
grep -c '"records": 1' "$GBLOG_ROOT/anchors/ops.json"                  # 0: replaced
```

**Fix:** in the loop, `if not ledger.log_files(b) and not os.path.exists(ledger._state_path(b)): continue`.

---

### D3-14 (Low) — assignment redaction stops at `&` and at quotes

**Where:** `_SECRET_PATTERNS`: the unquoted-value alternative `[^\s'"&]{6,}` in the env-style, keyword and glued-name rules.

```python
import gblog
print(gblog.redact("DB_PASSWORD=Sup3r&Secret!"))        # unchanged: "Sup3r" is shorter than 6
print(gblog.redact("password=abcdef&ghijkl9"))          # password=[REDACTED]&ghijkl9  (the tail survives)
```

The `&` stop exists for query strings, but it also applies to `.env` lines and command text, and from there the text reaches the ledger, the archive and git. SECURITY.md's "Not covered" list does not mention it.

**Fix (additive):** give the env-style rule a replacement function. When the character after the match is `&`, `'` or `"`, and the whitespace-delimited token that contains the match has no `?` before the match and no `://`, extend the redaction to the end of that token. Query strings keep today's output, and `name=[REDACTED]` stays idempotent.

Whether or not the code changes, add to "Not covered": "an unquoted value that contains `&` or a quote: the part after it is kept, and nothing is redacted when the part before it is shorter than 6 characters".

---

### D3-15 (Low) — documentation drift in this area

| File | Statement | Code |
|---|---|---|
| DESIGN §13, FAQ 35 | sync re-hashes on every run; there is no cache | `.git/gblog-sync-cache.json` (D3-05) |
| README, SECURITY.md, CHANGELOG, FAQ 25 | "the files under `logs/`, `anchors/` and `archive/` are compared" | Only `anchors/*.history.jsonl`; `anchors/<bot>.json` and `anchors/days/*.json` are force-added but not compared. Say "log files, anchor-history files and archived files", as DESIGN §13 does |
| DESIGN §6, `sync` record | target `git` or `git push`; outcome `pending` or `denied` | Also `git remote` / `ok` (origin change) and `git` / `fail` (drift) |
| SECURITY.md | "Use the literal `github.com` host … so that the check does not depend on ssh configuration" | A `Host github.com` block with another `HostName` redirects a literal ssh URL too. Say "use an `https://` URL", or resolve GitHub-literal ssh URLs with `ssh -G` and treat a non-GitHub result as another host |
| DESIGN §12, SECURITY.md | a symlinked sub-directory "is still followed" | sync cannot back it up (D3-08) |
| CHANGELOG "Known open findings", SECURITY.md "Supported versions" | sync stamps invalid chain directories (T-09); `--version` prints 0.6.0 | `sync` uses `valid_chains()`; `__version__ = "0.6.1"`. The other entries in that block look stale too (`--record-baseline`, hook v2, `_exit2`) |
| SECURITY.md table | no row for wiki remotes or for a missing repo-level origin | D3-02, D3-03 |

---

### D3-16 (Low) — small robustness items

1. **`-m " "`.** `redact(" ")` is truthy, so git aborts with "empty commit message" and sync exits 2 after the `sync pending` record. Fix: `commit_msg = (redact(message) or "").strip() or "gblog sync %s" % now_iso()`.
2. **"nothing to commit".** Only one of git's three wordings is matched ("nothing added to commit but untracked files present" and "no changes added to commit" are not). Fix: test `_git(root, "diff", "--cached", "--quiet", check=False).returncode == 0` before committing, and skip the commit.
3. **Exit 3 by string prefix.** The exit code comes from `res["error"].startswith("refused:")`, while `error` can also be the last 400 characters of git's stderr. Fix: set `result["refused"] = True` and test that.
4. **No lock around `sync`.** Two concurrent runs collide on `.git/index.lock`; one exits 2 after writing its record. Fix: hold `state/.sync.lock` for the whole call, as `anchor` holds `.anchors.lock`. The order is `.sync.lock` first, then bot locks.
5. **Legacy chain named `capture-files-<x>`.** Its `state/capture-files-<x>.json` matches the snapshot pattern: it is ignored, skipped by `_sync_evidence`, and removed from the index by `git rm --cached`. The state witness of that chain is never backed up. Fix: treat a `capture-files-*.json` file as state when `logs/<name>` exists, and name the files for `rm --cached` explicitly.
6. **Unborn `HEAD`.** If the first commit has nothing to commit, `rev-parse --short HEAD` raises and sync exits 2 with git's "unknown revision" text. Fix: return `commit: "-"` and "nothing committed".

---

## Simplifications

Unless noted, these do not change behaviour.

| # | Change | Effect |
|---|---|---|
| S-1 | Delete the sync cache (D3-05) | −35 lines; only the cost changes |
| S-2 | One `_GITIGNORE_BODY` constant and one `Ledger._write_gitignore()`, used by `init()` and `_sync_evidence()` | Removes a duplicated literal and the `exists` / `lexists` difference |
| S-3 | `--remote` block: one `config --local --get-all` read and `_set_origin` (D3-11) | Removes the `git remote` call and two branches |
| S-4 | Drop `allow_unknown = bool(allow_unknown)` | One line |
| S-5 | `worst` and `result["visibility"]` are read by neither the CLI nor a test; drop them and keep `visibilities` | Removes two return keys a library caller might read |
| S-6 | A bytes variant of `_git` for `_sync_ignored` and `_sync_drift` | Removes two hand-built argv/env copies |
| S-7 | Drop `-u` from the push; gblog never pulls | Also stops rewriting `branch.<name>.*` in `.git/config` on every run |

---

## Test cases to add

All use temporary directories, local bare repositories, `GIT_ALLOW_PROTOCOL=file`, and fakes for `remote_visibility`, `gh`, `urlopen` and `ssh`.

**`tests/test_security_regressions.py` (RemoteGuardTest)**

1. `test_bracketed_and_helper_urls_are_github`: for `[github.com:22]:o/r.git`, `git@[github.com]:o/r.git`, `[git@github.com:22]:o/r.git`, `https::https://github.com/o/r.git`, `ssh://git@github%2Ecom/o/r.git`, `https://ｇｉｔｈｕｂ.com/o/r`, `https://github。com/o/r` and `ssh://git@[github.com]/o/r.git`, `github_remote(u)[0]` is true and the owner is `None`.
2. `test_sync_refuses_odd_github_forms`: with visibility unknown, `sync --remote '[github.com:22]:o/r.git'` exits 3.
3. `test_decoded_authority_is_not_reported_private`: `ssh://git@evil.example%2F@github.com/o/r.git` is never looked up as `o/r`.
4. `test_unparsable_url_fails_closed`: a `ValueError` from parsing gives `(True, None, None)`.
5. `test_plain_forms_still_map`: the existing ACCEPT list still returns `(True, "deximple", "grokledger")`, and the look-alike and path-mention cases stay `(False, None, None)`.
6. `test_wiki_is_checked_as_its_repository`: the lookup asks for `repos/octocat/Hello-World`; with a public parent, `sync --remote …/Hello-World.wiki.git` exits 3.
7. `test_no_repo_origin_is_never_pushed`: three subtests (a bare repository at `<root>/origin`, `.git/remotes/origin`, `[remote "origin"]` in a `GIT_CONFIG_GLOBAL` file). Each gives exit 1, `(not pushed)`, an empty target repository and no visibility call.
8. `test_env_config_cannot_redirect_push`: with `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0=remote.origin.pushurl`, the push goes to the configured origin only.
9. `test_git_dir_in_environment_is_ignored`: with `GIT_DIR` pointing at another repository, that repository gets no commit and the ledger gets its own `.git`.
10. `test_literal_github_ssh_url_redirected_by_ssh_config`: with `ssh -G github.com` mocked to answer `hostname evil.example`, the URL is not reported as a confirmed-private GitHub repository.

**`tests/test_capture_sync.py`**

11. `test_sync_with_ignored_top_level_directory`: with `logs/` in `.git/info/exclude`, and with `archive/` in a global excludes file, sync exits 0 and the new day file and the archive are in `HEAD`.
12. `test_same_size_rewrite_with_restored_mtime_is_drift`: the D3-05 sequence gives exit 1 and names the file.
13. `test_remote_hook_output_with_invalid_bytes`: a `post-receive` hook that prints `\xff`; sync exits 0, prints `pushed`, and `last_sync.pushed` is true.
14. `test_commit_signing_config_does_not_break_sync`: `commit.gpgsign=true` and `gpg.program=false` in `GIT_CONFIG_GLOBAL`; sync exits 0.
15. `test_symlinked_archive_dir_is_named_in_the_error`: exit 1, and the message contains "symbolic link" and does not mention "ignore rule".
16. `test_reserved_sync_bot_does_not_change_origin`: `sync --bot aliases --remote X` exits 2 and `origin` is not set.
17. `test_origin_change_is_rolled_back_when_the_record_fails`: with `Ledger.append` mocked to raise `OSError`, `origin` is the previous URL afterwards.
18. `test_same_remote_with_two_urls_is_a_noop`: an origin with `[a, b]`; `--remote b` exits 0 and both URLs remain.
19. `test_replace_remote_clears_pushurl_and_extra_urls`: after `--replace-remote c`, `get-url --push --all` is `[c]`.
20. `test_remote_argument_is_stripped`: `--remote " <bare> "` twice exits 0 both times.
21. `test_token_as_username_warns_and_is_scrubbed`: the warning is printed; a mocked push error with `https://<token>@host` appears in neither stdout nor the ledger; the same holds for the `git failed:` path.
22. `test_empty_chain_dir_is_not_stamped`: the D3-13 sequence; `state/ops.json` is not created and `anchors/ops.json` still names the head.
23. `test_sync_does_not_stamp_invalid_chain_dirs`: with `logs/my bot`, neither `state/my bot.json` nor `state/my-bot.json` exists afterwards. The first round-3 audit named this test (W-05); it was never added.
24. `test_whitespace_commit_message_uses_default`: `-m " "` exits 0 and the subject starts with `gblog sync`.
25. `test_nothing_staged_is_not_an_error`: with an untracked file in the root and nothing staged, sync does not exit 2.
26. `test_concurrent_sync_runs_do_not_collide`: the second run waits and exits 0 (after item 4 of D3-16).
27. `test_legacy_capture_files_chain_state_is_committed`: a chain `capture-files-legacy` with its state file; the state file is in `HEAD` after sync.

**`tests/test_redaction_extended.py`**

28. `test_unquoted_value_with_ampersand`: `DB_PASSWORD=Sup3r&Secret!` and `password=abcdef&ghijkl9` are fully redacted; `https://h/x?token=abc&x=1&api_key=zzz` keeps today's output; all are idempotent.
29. `test_safe_url_on_free_text`: `_safe_url` scrubs `https://<anything>@host` inside a sentence and leaves `https://host/path@x` alone.

**`tests/test_round3_followups.py`**

30. `test_git_env_drops_repository_and_config_injection_variables`: `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_CONFIG_COUNT` and `GIT_CONFIG_KEY_0` are absent; `GIT_CONFIG_GLOBAL`, `GIT_ALLOW_PROTOCOL` and `LANG` are kept; `LC_ALL` is `C`.
31. `test_git_output_is_decoded_leniently`: `_git` with a fake `subprocess.run` receives `errors="replace"`.