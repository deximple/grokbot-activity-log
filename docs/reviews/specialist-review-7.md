# Specialist review K7 — 2026-11-16

**Area:** portability — macOS (BSD userland, bash 3.2, APFS), Python 3.8 → 3.13/3.14 differences, non-UTF-8 locales, paths with spaces or non-ASCII characters, unusable or read-only `HOME`, `install.sh`, `bin/gblog`, CI matrix.

**Method:** code reading of the current tree (`gblog.py`, `install.sh`, `bin/gblog`, `examples/*.sh`, `.github/workflows/ci.yml`, tests). Nothing was executed. Reproductions are derived from the code; where a statement depends on platform behaviour I could not confirm by reading, it is marked *(to confirm)*.

**Out of scope (owned by the round-4 G1/G2 work, not repeated here):** sync foreign-repo guard, tail `ts` bound, sync cache removal, side-file rule, task-status function, origin definition, rollup protection, hook fallback.

## Summary

| Id | Sev. | Platform | One line |
|---|---|---|---|
| K7-01 | High | macOS → any | AppleDouble `._*` sidecars are read as day files and spool events: `verify --all` FAILs every chain after a documented move; phantom `ALERT` |
| K7-02 | Medium | Linux (case-sensitive) | `_disk_name` merges `OPS`/`ops` on a case-**sensitive** file system; `verify --bot ops` verifies `OPS` |
| K7-03 | Medium | Linux `sh`/dash, C locale | `install.sh` persists `printf %q` output; the `$'…'` form is not POSIX, so `~/.profile` exports a wrong `GBLOG_ROOT` and gblog starts a second, empty ledger |
| K7-04 | Medium | macOS | Stock macOS (zsh, no rc files): `install.sh` creates `~/.profile`, which zsh never reads; `ZDOTDIR` ignored; `capture shell-hook --install` writes an unread `~/.bashrc` |
| K7-05 | Medium | macOS | `os.fsync` does not reach the disk on macOS; the "line durable before state" ordering in DESIGN §4 needs `F_FULLFSYNC` |
| K7-06 | Medium | legacy / ASCII locales | `alias add --display <non-ASCII>` ends in `internal error`; a display name passed as `--bot` opens a variant chain; text is stored as `\udcXX` although the bytes were valid UTF-8 |
| K7-07 | Medium | containers, services | No usable home directory: gblog creates `./~/activity-log` or `/activity-log`; `install.sh` aborts half-way with `HOME: unbound variable` |
| K7-08 | Low–Med | Python ≥ 3.11.4 vs 3.8 | Remote classification uses `urlsplit` as an oracle: the same URL is exit 3 on one Python and a push on another |
| K7-09 | Low–Med | macOS | `capture files` logs `.DS_Store` / `._*`; NFD file names never match NFC self-reports (Hangul, accents) |
| K7-10 | Low–Med | macOS, Ubuntu | Firefox history is not found on macOS, nor as snap or flatpak; Safari not captured and not documented |
| K7-11 | Low–Med | all | `install.sh` replaces `$PREFIX/gblog` non-atomically while other Bots are calling it |
| K7-12 | Low | APFS / NTFS | Case-only collisions: no warning that `Ops` + `ops` cannot be restored on macOS; sync drift lookup is exact-case; `capture files` self-exclusion is a string compare |
| K7-13 | Low | all | `install.sh` does not recognise `~/…` spellings, trailing comments or `sh install.sh` |
| K7-14 | Low | macOS, cron | Example scripts: `GBLOG_ROOT=~/…` not expanded; bracket ranges are locale-dependent in bash 3.2; `CRON_TZ` is not portable |
| K7-15 | Low | old distros | Minimum git version is neither documented nor checked; tests are not isolated on git < 2.32 |
| K7-16 | Low | RHEL 8, macOS | Old or stub `python3` gives a `SyntaxError` or misleading message; launcher symlink loop; `_DAY_FILE_RE` uses `\d` and `$` |
| K7-17 | — | CI | Matrix gaps: stock macOS interpreter, bash 3.2, odd paths, real non-UTF-8 file-system encoding, case-insensitive assertions, sourcing of written rc lines, script jobs on macOS |
| K7-18 | Low | Windows checkouts | No `.gitattributes`: CRLF conversion breaks the bash scripts and the byte-compared fixtures |

---

## K7-01 (High) — AppleDouble `._*` files are treated as ledger files

**Where:** `gblog.py` — `Ledger.log_files`, `Ledger._tail_record`, `Ledger._file_findings`, `Ledger.spool_files`, `_sync_evidence`, `_GITIGNORE_BODY`.

```python
paths = (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl"))
return sorted(p for p in paths if os.path.isfile(p))
...
return sorted(os.path.join(d, f) for f in os.listdir(d)
              if f.endswith(".json") and os.path.isfile(os.path.join(d, f)))
```

**Problem.** macOS stores extended attributes as a sidecar file `._<name>` wherever the destination cannot hold them:

- inside a `tar` archive made with the system `tar`, which GNU tar on Linux extracts as real files;
- on exFAT/FAT volumes and many SMB/NFS shares.

On recent macOS many ordinary files carry an attribute (`com.apple.provenance`, `com.apple.quarantine`), so this is common *(how often gblog-written files carry one: to confirm on a Mac)*. FAQ 15 tells the operator to "copy the whole root". After such a move every chain directory holds `._2026-11-15.jsonl`, a regular file ending in `.jsonl` with a binary header.

- `log_files` returns it first (`.` sorts before digits). `verify` stops at line 1: `FAIL ops 0 records` / `- logs/ops/._2026-11-15.jsonl:1: not valid JSON`.
- `_tail_record` walks names in reverse, so appends still succeed with exit 0. The ledger keeps growing while every `verify` FAILs. The operator rule for FAIL is "do not edit log files".
- A `._x.json` in `spool/<bot>/` counts as a pending event: `status` prints `ALERT ops: tail mismatch since unknown (1 spooled)`, `spool replay` exits 2 forever, and `examples/routine-daily-audit.sh` exits 4 every night.
- `sync` commits the sidecars, so every clone of the backup inherits the FAIL.

**Reproduction (any OS):**

```bash
export GBLOG_ROOT=$(mktemp -d); gblog note --bot ops --detail x
f=$(ls "$GBLOG_ROOT"/logs/ops/*.jsonl)
printf '\000\005\026\007\000\002\000\000Mac OS X        ' > "$(dirname "$f")/._$(basename "$f")"
gblog verify --all                  # FAIL ops 0 records ... ._<day>.jsonl:1: not valid JSON   (exit 1)
gblog note --bot ops --detail y     # exit 0: the chain keeps growing
mkdir -p "$GBLOG_ROOT/spool/ops"; printf '\000\005\026\007' > "$GBLOG_ROOT/spool/ops/._x.json"
gblog status                        # ALERT ops: tail mismatch since unknown (1 spooled)
```

**Fix.** Recognise a sidecar by its content, not only its name, so a real day file renamed to `._x.jsonl` is still read and still FAILs as today:

```python
_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"

def _is_sidecar(path: str) -> bool:
    """A macOS metadata file that is never ledger content: `.DS_Store`, or `._name` starting with the AppleDouble magic."""
    name = os.path.basename(path)
    if name == ".DS_Store":
        return True
    if not name.startswith("._"):
        return False
    try:
        with _open_regular(path) as fh:
            return fh.read(4) == _APPLEDOUBLE_MAGIC
    except OSError:
        return False
```

1. `log_files`, `_tail_record`, `spool_files`: skip entries for which `_is_sidecar` is true. Only names starting with `._` cost an extra open.
2. `_file_findings`: add a layout finding `"<rel>: macOS AppleDouble file ignored (not a log file)"`. It is a `!` warning by default and a problem with `--strict`, like the symlink findings.
3. `_sync_evidence.take`: do not add sidecars to `forced`/`checked`. Untrack exactly those paths, next to the existing `git rm --cached` of lock files.
4. `_GITIGNORE_BODY`: add `.DS_Store` and `._*` for new ledgers.
5. FAQ 15: recommend `COPYFILE_DISABLE=1 tar …` (or `tar --no-mac-metadata`), and `dot_clean -m <root>` on exFAT.

Ignoring such a file gives an adversary nothing new: renaming a day file to hide it equals deleting it, which state, anchors and `--contains` already catch. The magic-byte test keeps a renamed JSONL file visible.

**Tests.**
- Sidecar with the magic beside a valid day file: `verify` is `OK` with one `!` line; `--strict` is exit 1 with the same text as `-`; `append`, `tail` and `report` are unchanged.
- A real JSONL line in `._2024-06-01.jsonl` (no magic): still `FAIL … not valid JSON` or a chain break, as today.
- `spool/ops/._x.json` with the magic: `status` prints no `ALERT`; `spool replay` exits 0; `alert_info == (None, 0)`.
- `sync --no-push`: `git ls-tree -r HEAD` has no `._*` path and the run exits 0.

---

## K7-02 (Medium) — `_disk_name` merges all-upper/all-lower spellings on a case-sensitive file system

**Where:** `gblog.py`, `Ledger._disk_name` (used by `write_name` and as the fallback of `chains_for`).

```python
other = bot.swapcase()
if other == bot or not os.path.isdir(os.path.join(self.logs_dir, other)):
    return bot
...
if bot in names:
    return bot
low = bot.lower()
return next((n for n in sorted(names) if n.lower() == low), bot)
```

**Problem.** "The swapcase spelling is a directory" is used as proof that the file system ignores case. On ext4 `logs/OPS` can simply exist. Then a write to the new chain `ops` finds `isdir(logs/OPS)` true and `"ops" not in names`, and is redirected to `OPS`. README and `docs/agent-integration.md` §7 promise the opposite: "On a case-sensitive file system names that differ only by letter case stay separate chains". The existing test uses `ops`/`Ops`, whose swapcase is `oPS`, so it never reaches the branch.

**Reproduction (Linux, empty alias registry):**

```bash
export GBLOG_ROOT=$(mktemp -d)
gblog note --bot OPS --detail first
gblog note --bot ops --detail second
ls "$GBLOG_ROOT/logs"        # OPS            (expected: OPS ops)
gblog tail                   # both records under OPS, "bot": "OPS"
gblog verify --bot ops       # OK OPS ... exit 0   (expected: FAIL ... no chain on disk for 'ops')
```

`Ops` then `ops` gives two chains; `oPS` then `Ops` gives one. The result depends on the spelling pair.

**Fix.** Ask the file system directly whether the exact spelling resolves, and accept another spelling only when it is the same directory:

```python
def _disk_name(self, bot: str) -> str:
    own = os.path.join(self.logs_dir, bot)
    if not os.path.isdir(own):               # nothing the file system equates with this name
        return bot
    try:
        names = os.listdir(self.logs_dir)
    except OSError:
        return bot
    if bot in names:
        return bot
    low = bot.lower()
    for n in sorted(names):
        if n.lower() == low:
            try:
                if os.path.samefile(os.path.join(self.logs_dir, n), own):
                    return n
            except OSError:
                pass
    return bot
```

On ext4 `isdir(logs/ops)` is false while only `OPS` exists, so the name stays `ops`. On APFS it is true and `ops` is not in the listing, so `samefile` picks the on-disk spelling.

**Tests.**
- Case-sensitive (skip when a probe shows insensitivity): `OPS` then `ops` gives two directories and two `bot` values; `verify --bot ops` with only `OPS` on disk exits 1 with `no chain on disk for 'ops'`; the same for the pairs `a1`/`A1` and `oPS`/`Ops`.
- Case-insensitive (run when the probe shows insensitivity — the macOS CI job; today the only real-FS test is skipped there): seed `Ops`, `alias add --slug ops`, `note --bot ops`. Expect one directory `Ops`, the new record has `bot == "Ops"`, and `verify` shows no `bot field` warning.
- Update `test_disk_name_unit` to patch `os.path.samefile` as well.

---

## K7-03 (Medium) — `install.sh` writes `printf %q` output into files that `sh` reads

**Where:** `install.sh`.

```bash
path_line="export PATH=$(printf %q "$PREFIX"):\"\$PATH\""
root_line="export GBLOG_ROOT=$(printf %q "$GBLOG_ROOT")"
...
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do
```

**Problem.** `printf %q` is a bash-ism for bash's own re-reading. For a string with a byte bash considers non-printable it emits ANSI-C quoting, `$'…'`:

- any non-ASCII byte when the script runs in the C locale (ssh one-liners, CI, tool shells);
- possibly also in UTF-8 locales on bash older than 4.2, i.e. macOS `/bin/bash` 3.2 *(to confirm)*;
- any tab or newline.

`~/.profile` is read by `sh`. On Debian and Ubuntu that is dash, which has no `$'…'` *(current dash releases: to confirm)*. Dash reads `export GBLOG_ROOT=$'/home/u/caf\303\251/ledger'` as the literal value `$/home/u/caf\303\251/ledger`. That is a relative path. gblog then creates `<cwd>/$/home/u/caf\303\251/ledger`, prints `notice: initialized new ledger`, and logs there. That is the "second, empty ledger" failure the docs warn about.

Two smaller effects:

- A rerun under another locale produces a different `root_line`. `grep -qxF -- "$root_line"` misses, and the script prints "already has a '# gblog ROOT' block with a different value" for the same directory.
- `is_this_root` has the same blind spot for unmarked lines.

**Reproduction (Debian/Ubuntu):**

```bash
T=$(mktemp -d); mkdir -p "$T/home" "$T/café"; : > "$T/home/.profile"
HOME="$T/home" GBLOG_PREFIX="$T/bin" GBLOG_ROOT="$T/café/ledger" LC_ALL=C bash ./install.sh
grep GBLOG_ROOT "$T/home/.profile"                 # export GBLOG_ROOT=$'/tmp/.../caf\303\251/ledger'
HOME="$T/home" dash -c '. "$HOME/.profile"; printf "%s\n" "$GBLOG_ROOT"'   # $/tmp/.../caf\303\251/ledger
```

**Fix.** Write POSIX single-quote syntax, which every shell reads the same way. Leave plain paths bare, so lines written by 0.6.0/0.6.1 stay byte-identical and reruns remain no-ops:

```bash
sq() {  # sq STRING: STRING as one POSIX-quoted word (sh, dash, bash 3.2+, zsh, ksh)
  local s=$1 out=""
  case "$s" in
    ''|*[!A-Za-z0-9_./+:@%,-]*) ;;
    *) printf '%s' "$s"; return ;;
  esac
  while [ "${s#*\'}" != "$s" ]; do out="$out${s%%\'*}'\\''"; s=${s#*\'}; done
  printf "'%s%s'" "$out" "$s"
}
path_line="export PATH=$(sq "$PREFIX"):\"\$PATH\""
root_line="export GBLOG_ROOT=$(sq "$GBLOG_ROOT")"
```

- Set `LC_ALL=C` for the script's own pattern matching, so `[A-Za-z0-9…]` is an ASCII range in bash 3.2 too (see K7-14).
- Keep `$(printf %q …)` in `path_forms`/`root_forms`, so older lines are still recognised.
- Let `is_this_root` strip one level of `'…'` and `$'…'` (decode the latter with `printf '%b'`).
- For an existing marker block, compare its *value* through `is_this_root` instead of the whole line.

**Tests (extend `tests/test_scripts.py`).** The `_PLAIN` guard currently skips exactly the cases that matter.
- Install with a root and prefix containing a space, `é`, `'` and `$`. For each available shell of `sh`, `dash`, `bash`, `zsh`, run `HOME=… <shell> -c '. "$HOME/<rc>" && printf %s "$GBLOG_ROOT"'` and expect the absolute root byte for byte; the same for the first `PATH` entry.
- The same under `LC_ALL=C` and under a UTF-8 locale. Rerun in the *other* locale: rc file unchanged (`cmp`), no "different value" note.
- An rc file that already holds the old `%q` spelling: no warning, no second block.

---

## K7-04 (Medium) — stock macOS: the files gblog writes are never read

**Where:** `install.sh` (rc selection); `gblog.py`, `capture shell-hook` (`--rc` default `~/.bashrc`).

```bash
for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do ...
if [ "$found" = 0 ]; then
  persist "$HOME/.profile"  # no rc file exists yet: create ~/.profile
fi
```

**Problem.**

1. A fresh macOS account has zsh as login shell and none of the four files. `install.sh` creates `~/.profile` and says "open a new shell to pick up the change". zsh never reads `~/.profile`. `gblog` is not on `PATH`. A custom `GBLOG_ROOT` is not exported, so a call by absolute path silently uses `~/activity-log` and initialises a second ledger.
2. The same happens when only `~/.profile` exists and the login shell is zsh.
3. `ZDOTDIR` is ignored: zsh reads `$ZDOTDIR/.zshrc`. `tests/test_scripts.py` even unsets `ZDOTDIR`.
4. macOS Terminal starts *login* shells. bash then reads `~/.bash_profile` (or `~/.profile`), not `~/.bashrc`. A Mac with only `~/.bashrc` gets the block where it is never read.
5. `gblog capture shell-hook --install` appends the bash-only hook to `~/.bashrc` and prints `installed hook in …`. On stock macOS nothing is captured, and coverage then reads as "nothing to observe" rather than as a logging gap.

**Reproduction (macOS, or anywhere with `SHELL=/bin/zsh`):**

```bash
T=$(mktemp -d); mkdir "$T/home"
HOME="$T/home" SHELL=/bin/zsh GBLOG_PREFIX="$T/bin" GBLOG_ROOT="$T/ledger" bash ./install.sh
ls -A "$T/home"                                   # .profile only
HOME="$T/home" zsh -ic 'echo "${GBLOG_ROOT:-unset}"; command -v gblog || echo no-gblog'
# unset / no-gblog
```

**Fix.**

- `install.sh`: use `"${ZDOTDIR:-$HOME}/.zshrc"` in the loop. After the loop, make sure the *login shell's own* file received the block:

```bash
zrc="${ZDOTDIR:-$HOME}/.zshrc"
case "${SHELL##*/}" in
  zsh)  [ -f "$zrc" ] || persist "$zrc" ;;
  bash) if [ "$(uname -s)" = Darwin ] && [ ! -f "$HOME/.bash_profile" ] && [ ! -f "$HOME/.profile" ]; then
          persist "$HOME/.bash_profile"; fi ;;
esac
[ "$found" = 0 ] && [ "$modified" = 0 ] && [ "$failed" = 0 ] && persist "$HOME/.profile"
```

  Linux with bash and no rc files still gets `~/.profile`, so existing tests and the CI greps hold.
- `gblog.py`, `capture shell-hook --install`: stdout unchanged; add stderr lines.
  - `$SHELL` ends in `zsh`: `note: this hook is for interactive bash; your login shell is zsh, whose commands are not captured`.
  - `sys.platform == "darwin"` and the rc file is `~/.bashrc`: `note: Terminal starts login shells, which read ~/.bash_profile; source ~/.bashrc from it or pass --rc ~/.bash_profile`.
- README ("What capture does not see") and `skills/activity-log/SKILL.md`: one sentence about macOS.
- A zsh hook (`precmd` plus `fc -ln -1`) would be a new feature. It is the largest capture gap on macOS and is recorded here as a follow-up, not designed.

**Tests.**
- `test_scripts.py`: pin `SHELL=/bin/sh` in `_env()` so existing tests are deterministic.
- `SHELL=/bin/zsh`, no rc files: `.zshrc` is created with both blocks and a rerun is a no-op.
- `ZDOTDIR=$T/z`: the blocks land in `$T/z/.zshrc`.
- `SHELL=/bin/zsh` with only `.profile`: `.zshrc` is created and `.profile` also gets the blocks.
- Hook: with `SHELL=/bin/zsh`, `--install` prints the note on stderr; stdout is byte-identical.

---

## K7-05 (Medium) — `fsync` is not a durability barrier on macOS

**Where:** `gblog.py` — `_append_line` (`os.fsync(fd)`), `_atomic_write` (`os.fsync(fh.fileno())`), `_fsync_dir`.

**Problem.** DESIGN §4 builds the recovery argument on ordering: the line is made durable (step 6) before state is replaced (step 7). On macOS `fsync(2)` only hands the data to the drive; it does not flush the drive's write cache. That needs `fcntl(fd, F_FULLFSYNC)`. After a power loss the state rename can survive while the log line does not. That is the "state ahead of the log" shape, which the design says "cannot come from a crash inside `append`" and which is always refused (exit 4, "restore the log from backup"). On a laptop running on battery this is not exotic. The doc claim is true on Linux and not guaranteed on macOS.

**Fix.**

```python
def _fsync(fd: int) -> None:
    """fsync that reaches the disk: on macOS fsync(2) stops at the drive cache, F_FULLFSYNC does not."""
    full = getattr(fcntl, "F_FULLFSYNC", None) if fcntl is not None else None
    if full is not None and os.environ.get("GBLOG_FULLFSYNC", "1") != "0":
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass          # file system without support (SMB, some FUSE): fall back
    os.fsync(fd)
```

- Use it in `_append_line`, `_atomic_write` and `_fsync_dir`.
- Document the cost (several milliseconds per call, three calls per append) and the opt-out `GBLOG_FULLFSYNC=0` in DESIGN §4 and §13.
- Add a row to SECURITY.md "Filesystem assumptions".
- If the maintainer prefers not to pay the cost, the minimum is to make the documented guarantee platform-specific.

**Tests.**
- `test_append_fsync_order` patches `gblog.os.fsync`; with the helper it would see no events on macOS. Patch `gblog._fsync` instead and keep the order assertions.
- With a fake `fcntl.F_FULLFSYNC` attribute and `fcntl.fcntl` raising `OSError(ENOTSUP)`, the append still succeeds and `os.fsync` is called.
- With `GBLOG_FULLFSYNC=0`, `fcntl.fcntl` is not called.

---

## K7-06 (Medium) — arguments that the locale could not decode

**Where:** `gblog.py` — `_clean_text`, `Ledger._add_alias` / `_write_aliases`, `Ledger.resolve`.

```python
def _clean_text(value):
    ...
        except UnicodeEncodeError:
            return value.encode("utf-8", "backslashreplace").decode("utf-8")
...
_atomic_write(path, lambda fh: json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False))
```

**Background.** With `LC_ALL=C`, Python ≥ 3.7 switches to UTF-8 mode. The CI step "C locale, ASCII stdio" therefore still runs with a UTF-8 *file-system* encoding and tests only stdio. A different file-system encoding occurs with a real legacy locale (`ko_KR.eucKR`, `ja_JP.eucJP`, `*.ISO-8859-1`) or with `PYTHONUTF8=0`. Bytes that the locale codec cannot decode then arrive in `sys.argv` as lone surrogates. The common case is UTF-8 bytes sent by an agent into a legacy-locale shell.

**Problems.**

1. `alias add --display "Büro Chef"`: the display name is not passed through `_clean_text`. `json.dump(..., ensure_ascii=False)` into a UTF-8 file raises `UnicodeEncodeError` (surrogates not allowed). The user sees `error: internal error (UnicodeEncodeError: …)`, exit 1 — the code reserved for an integrity FAIL.
2. `--bot "Büro Chef"` for a registered display name: `resolve()` compares the surrogate-carrying string with the stored one and finds no match. The name is sanitised to `B-ro-Chef` and a new variant chain is opened (or the write is refused under `alias strict on`).
3. `--detail "café"` is stored as the text `caf\udcc3\udca9`. The bytes were valid UTF-8 and could have been recovered exactly.

**Reproduction (Linux, UTF-8 terminal):**

```bash
export GBLOG_ROOT=$(mktemp -d) PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 LC_ALL=C
gblog alias add --slug buero --display "Büro Chef"; echo $?   # error: internal error (UnicodeEncodeError ...)  1
gblog note --bot buero --detail "café"; gblog export          # "detail": "caf\\udcc3\\udca9"
```

**Fix.** Recover the original bytes first, and escape only what is really not UTF-8:

```python
def _clean_text(value):
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            try:   # argv bytes this locale could not decode: they are valid UTF-8 far more often than not
                return os.fsencode(value).decode("utf-8")
            except (UnicodeError, ValueError):
                return value.encode("utf-8", "backslashreplace").decode("utf-8")
    ...
```

- `"bad \udcff byte"` still becomes `bad \udcff byte`, so the existing surrogate tests hold.
- Apply `_clean_text` once in `_main`, right after `_validate_args`, to the *text* arguments: `bot`, `task`, `task_id`, `detail`, `target`, `display`, `slug`, `name`, `message` and each `--alias`.
- Do **not** apply it to path arguments (`--root`, `--file`, `--path`, `--rc`, `--history`, `--exclude`). They must keep `surrogateescape` to address files.
- Belt and braces: `display = _clean_text(display.strip())` in `_add_alias`.

**Tests** (subprocess with `PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 LC_ALL=C`; skip on macOS and Windows, where the file-system encoding is always UTF-8; assert `sys.getfilesystemencoding() != "utf-8"` in the child first):
- `alias add --display "Büro Chef"` exits 0 and `aliases.json` holds `"Büro Chef"`.
- `note --bot "Büro Chef" --detail "café ☕"` lands in chain `buero` with exactly that detail.
- `status`, `tail` and `export` print only ASCII (the existing escaping rule).
- `--detail` with the bytes `caf\xff` is still stored as `caf\udcff`.

---

## K7-07 (Medium) — no usable home directory

**Where:** `gblog.py` — `default_root`, `Ledger.__init__`; `install.sh`.

```python
return os.path.join(os.path.expanduser("~"), "activity-log")
...
self.root = os.path.abspath(os.path.expanduser(root or default_root()))
```

```bash
case "$PREFIX" in
  "$HOME"/*) path_forms+=( ... ) ;;       # with `set -u`
```

**Problem.**

- **`HOME` unset and no passwd entry** (`docker run --user 12345`, OpenShift, some service managers). `os.path.expanduser("~")` returns `"~"` unchanged. `default_root()` is `~/activity-log`, `abspath` makes it `<cwd>/~/activity-log`, and the first write creates a directory literally named `~` in the working directory and starts a ledger there. The same applies to `--rc ~/.bashrc`.
- **`HOME=""`.** `expanduser("~")` returns `/`. The default root becomes `/activity-log`: created when running as root, `Permission denied` otherwise.
- **`install.sh` with `HOME` unset**, even with `GBLOG_PREFIX` and `GBLOG_ROOT` given. The binary is installed and `gblog init` has run. Then the script dies on the first unconditional `"$HOME"` with `HOME: unbound variable`, exit 1, before the "ledger root:" lines. With `HOME=""` and no overrides it installs into `/.local/bin` and `/activity-log` when run as root.
- `examples/*.sh` already do this correctly (`home="${HOME:-}"`).

**Reproduction:**

```bash
docker run --rm --user 12345 -v "$PWD":/src -w /tmp python:3.12-slim \
  sh -c 'unset HOME; python /src/gblog.py note --bot ops --detail x; ls -d /tmp/~/activity-log'
env -u HOME GBLOG_PREFIX=/tmp/p GBLOG_ROOT=/tmp/l bash ./install.sh; echo $?   # ... HOME: unbound variable / 1
```

**Fix.**

- `default_root()`: accept the home directory only when `os.environ.get("HOME") != ""` and `expanduser("~")` is absolute. Otherwise `_die("cannot determine a home directory (HOME is unset or empty); set GBLOG_ROOT or pass --root")`.
- `Ledger.__init__`: if the path still starts with `~` after `expanduser`, `_die("'%s' cannot be expanded here; write ./%s for a directory of that name" …)`. Use the same check for `--rc`.
- `install.sh`: start with `HOME="${HOME:-}"`. When it is empty, require both `GBLOG_PREFIX` and `GBLOG_ROOT` (otherwise a one-line error before anything is installed) and skip persistence with the existing "no shell startup file could be written" message, exit 0.

**Tests.**
- `mock.patch.object(gblog.os.path, "expanduser", lambda p: p)` with `GBLOG_ROOT` unset: `run_raw(["status"])` exits 2 with the message and creates no `~` entry in the working directory.
- `HOME=""`: exit 2.
- `Ledger("~nosuchuser/x")` raises `SystemExit(2)`.
- `env -u HOME GBLOG_PREFIX=… GBLOG_ROOT=… bash install.sh`: exit 0, binary present, stdout contains "no shell startup file could be written".
- Without the two variables: exit 1, nothing installed.

---

## K7-08 (Low–Medium) — the same remote URL is classified differently by different Pythons

**Where:** `gblog.py`, `_remote_host_path`.

```python
try:
    urlsplit(u).hostname  # a URL the standard parser rejects is not one gblog classifies as harmless
except ValueError:
    return None, None, "invalid"
```

**Problem.** `urlsplit` is used as a rejection oracle, and its rules changed. From about 3.11.4/3.12 it validates bracketed hosts and raises `ValueError` for `[name]` that is not an IP literal *(exact patch levels of older branches: to confirm)*. git accepts brackets around host names. For `ssh://git@[git.example.com]:2222/o/r.git`:

| Python | `_remote_host_path` | `github_remote` | `sync` |
|---|---|---|---|
| 3.8 … 3.11.3 | host `git.example.com`, kind `odd:ssh` | `(False, None, None)` | warns, pushes |
| ≥ 3.11.4 | kind `invalid` | `(True, None, None)` | **exit 3**, "could not confirm … is private" |

The same ledger and `origin` back up from the Python 3.8 Bot computer and are refused from a laptop with Python 3.12. The refusal blames GitHub visibility for a host that is not GitHub. GitHub hosts are unaffected: both paths fail closed.

**Fix.** Keep the oracle for everything except this one version-dependent rule, and decide that rule with gblog's own bracket handling, which is already there:

```python
except ValueError:
    bracketed = "[" in authority and authority.count("[") == authority.count("]") == 1
    if not (bracketed and _host_of(authority)):
        return None, None, "invalid"
    odd = True      # a bracketed host: classified by its parsed name, on every Python
```

**Tests** (a table run on every matrix interpreter — this guards against the next `urllib` change):

| URL | expected `github_remote` |
|---|---|
| `ssh://git@[git.example.com]:2222/o/r.git` | `(False, None, None)` |
| `ssh://git@[github.com]:22/o/r.git` | `(True, None, None)` |
| `https://[github.com/o/r` | `(True, None, None)` |
| `https://[::1]:8443/o/r.git` | `(False, None, None)` |

---

## K7-09 (Low–Medium) — macOS noise and Unicode normalisation in capture and coverage

**Where:** `gblog.py` — `capture_files` (`skip_names`), `norm_target`, `Ledger.resolve`, `_norm_title`, `_title_key`.

```python
skip_names = set(exclude) | {".git", "__pycache__", "node_modules", ".cache", ".venv"}
...
if kind == "file":
    return os.path.basename(t.rstrip("/"))
```

**Problems.**

1. Finder rewrites `.DS_Store` whenever a folder is viewed. Every `capture files` run on a Mac logs `.DS_Store created/modified` (and `._*` sidecars on exFAT) as `auto` file actions that no Bot reports. File coverage drops, and `tune` then proposes a `logging` change for a gap that does not exist.
2. `os.walk` returns names as stored. HFS+ and many Cocoa save paths store NFD (decomposed). For Korean that is the familiar jamo separation: `보고서.csv` typed by the Bot is NFC, the name from disk is NFD. `norm_target` compares raw strings, so the pair never matches and the action counts as unreported. `é` behaves the same.
3. The same mismatch affects `resolve()` (a display name copied from a file name), `_norm_title` and `_title_key` (routine detection and the twin hint).

**Fix.**

- `capture_files`: add `.DS_Store`, `.localized`, `.Spotlight-V100`, `.fseventsd`, `.Trashes`, `.TemporaryItems`, `Thumbs.db` and `desktop.ini` to the default skip names. Skip `._*` names for which `_is_sidecar` (K7-01) is true.
- Comparison keys only (stored records stay as observed): apply `unicodedata.normalize("NFC", …)` in `norm_target`, in `resolve()` to `raw` and to each compared display/alias, in `_norm_title` and in `_title_key`.

**Tests.**
- `coverage` with a self-report `보고서.csv` (NFC) and an auto record `unicodedata.normalize("NFD", "보고서.csv")`: matched 1/1; the same for `café.txt`.
- `capture files` on a tree with `.DS_Store` and a magic-prefixed `._a.txt`: `0 change(s)`.
- A plain file named `._notes` without the magic is still captured.
- `alias add --display "뷔로"`, then `resolve(NFD form)` returns the slug.

---

## K7-10 (Low–Medium) — browser history locations

**Where:** `gblog.py`, `BROWSER_HISTORY_GLOBS`.

**Problem.** The skill text promises "Chromium family and Firefox". The only Firefox glob is `~/.mozilla/firefox/*/places.sqlite`. Not found:

- Firefox on macOS (`~/Library/Application Support/Firefox/Profiles/*/places.sqlite`);
- Firefox as a snap — the default on Ubuntu 22.04+ (`~/snap/firefox/common/.mozilla/firefox/*/places.sqlite`);
- Firefox as a flatpak (`~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/places.sqlite`);
- Vivaldi and Arc on macOS;
- Safari — the default macOS browser — is not captured at all and the README does not say so.

On those systems `capture browser` exits 1 ("no browser history database found") or imports only part of the activity.

**Fix.** Add the globs above, plus `~/Library/Application Support/Vivaldi/*/History` and `~/Library/Application Support/Arc/User Data/*/History`. Add one README bullet under "What capture does **not** see": Safari uses another schema and needs Full Disk Access. If Safari support is wanted later, it is a third reader beside the Chrome and Firefox ones.

**Tests.** With `HOME` pointed at a temp directory, create an SQLite fixture at each new location and assert `find_browser_histories()` contains it. Filter the result to paths under the temp `HOME`, because the absolute globs may match the developer's real profiles. Run one capture from the macOS Firefox location and expect `added == 1`.

---

## K7-11 (Low–Medium) — the installed binary is replaced non-atomically

**Where:** `install.sh`: `install -m 0755 "$tmp" "$PREFIX/gblog"`.

**Problem.** GNU and BSD `install` both unlink (or truncate) the target and then copy. Parallel Bots on one computer are a design point, and the upgrade instruction is `git pull && bash install.sh`. A Bot that calls `gblog` in that window gets "command not found" or a truncated script (`SyntaxError`). A logging call fails. The shell hook discards the error, so the event is lost without a trace.

**Fix.**

```bash
new="$PREFIX/.gblog.new.$$"
trap 'rm -f "$tmp" "$new"' EXIT
install -m 0755 "$tmp" "$new"
mv -f "$new" "$PREFIX/gblog"        # rename(2): callers see the old or the new file, never half of one
```

**Test.** In `test_scripts.py`, loop `"$PREFIX/gblog" --version` in a background process while `install.sh` runs 20 times. Every call exits 0 and prints the version line. Bounded to a few seconds; mark slow.

---

## K7-12 (Low) — case-insensitive file systems: remaining gaps

1. **No warning for chains that differ only in case.** A ledger from Linux with `logs/Ops` and `logs/ops` cannot be cloned or copied to APFS or NTFS. git reports colliding paths, the two chains' same-named day files overwrite each other, and `verify` FAILs with no hint at the cause. *Fix:* `verify --all` and `alias list` emit one `!` line per group, for example `chain names 'Ops' and 'ops' differ only in letter case: this ledger cannot be restored on a case-insensitive file system`. Add one sentence to FAQ 16. *Test:* two such chains on a case-sensitive file system give the warning and exit code 0.
2. **`_sync_drift` looks the path up exactly** (`tree.get(rel)`), while git on macOS runs with `core.ignorecase=true` and keeps the first-seen spelling in the index. After a case-only rename of a chain directory (`mv logs/Ops logs/ops`), every evidence file is "not in HEAD". `sync` then exits 1 on every run with "committed content differs from disk … (git ignore rule, filter or index flag)" — the wrong cause. *Fix:* when `git config --type=bool core.ignorecase` is true, key `tree` by `casefold()` and look up `rel.casefold()`. *Test (macOS job):* commit, rename the directory in case only, `sync --no-push` exits 0.
3. **`capture_files` excludes the ledger by string comparison** (`_is_within(dirpath, ledger.root)` on `abspath`). On macOS `--path /private/tmp/w` with a root given as `/tmp/w/ledger`, or a different letter case, scans the ledger's own files. Every run then logs the previous run's writes. The symlinked `--path` case is already listed as D4-15; the macOS `/tmp` → `/private/tmp` alias and the case variant make it the default situation for anything under `/tmp` on a Mac. *Fix:* compare with `os.path.samefile(dirpath, ledger.root)` during the walk, or resolve both with `realpath` first. *Test:* root created through a symlinked parent, capture through the real path: no record with a target under the ledger directory.

---

## K7-13 (Low) — `install.sh` recognition and entry-point details

- **`~/…` spellings are not recognised.** `root_forms`/`path_forms` hold the absolute path, the `%q` form, `$HOME/…` and `${HOME}/…`. A hand-written `export GBLOG_ROOT=~/activity-log` — the most natural spelling — gives a warning that the line names a different root and no block. `export PATH=~/.local/bin:$PATH` gets a duplicate block. *Fix:* add `"~/${X#"$HOME"/}"` to both lists.
- **Trailing comments and `VAR=…; export VAR`.** `rc_root_value` takes the rest of the line, so `export GBLOG_ROOT=/x  # ledger` is "a different value". *Fix:* cut an unquoted ` #…` suffix before comparing; also accept `GBLOG_ROOT=…` without the `export` keyword.
- **`sh install.sh`** on Debian or Ubuntu dies on line 1 (`set: Illegal option -o pipefail`). *Fix:* put `[ -n "${BASH_VERSION:-}" ] || { echo "install.sh needs bash: run 'bash install.sh'" >&2; exit 1; }` before `set -euo pipefail`.
- **Tests:** one case per spelling. Expect no warning, no added block and an unchanged file.

---

## K7-14 (Low) — example scripts

- **`GBLOG_ROOT=~/activity-log` from cron or systemd is not expanded.** gblog expands it since D5-12. `routine-daily-audit.sh` tests `[ ! -d "$root/logs" ]` literally and exits 2 with "ledger missing" although gblog would have found the ledger. *Fix:* `case "$root" in "~") root="$home" ;; "~/"*) root="$home/${root#"~/"}" ;; esac` before the check. *Test:* run with `GBLOG_ROOT='~/ledger'` and `HOME=$T`; expect exit 0.
- **Bracket ranges depend on the locale in bash 3.2** (no `globasciiranges`). `*[!A-Za-z0-9._+-]*` in `agent-task.sh` (the "plain program name" rule) and `*[!A-Za-z0-9._-]*` in `print_heads` can accept accented letters under a UTF-8 locale on macOS. The stated rule "only a plain name is logged" then depends on the locale. *Fix:* `LC_ALL=C` in both scripts for their own matching; restore the caller's locale for the wrapped command in `agent-task.sh` (`env LC_ALL="$orig" … "$@"`, or scope the assignment to the `case`).
- **`compgen -G "$root/state/*.alert.json"`** treats glob characters in `$root` as a pattern. *Fix:* `for f in "$root"/state/*.alert.json; do [ -e "$f" ] && saw_alert=1; done`.
- **`CRON_TZ=UTC` in `examples/README.md`** is a cronie feature. macOS cron ignores it *(Debian cron: to confirm)*. The job then runs at 00:10 local time. The report is still a complete UTC day, only later than intended. *Fix:* say so and show the portable alternative: a fixed local hour, or `TZ=UTC` inside the command.

---

## K7-15 (Low) — git version assumptions

**Where:** `gblog.py` (`_git_call`, `sync`); `tests/_support.py` (`GIT_ENV`).

- `-c core.hooksPath=` needs git ≥ 2.9. Older git ignores it silently and **hooks run**, against the SECURITY.md statement.
- `commit --no-gpg-sign` *(≈ 2.0)* and `push --no-signed` *(≈ 2.2)* are hard errors before that.
- `remote get-url --push --all` needs ≥ 2.7. On older git it fails, `urls` is empty, and `sync` reports "no origin configured in the ledger repository" — the wrong cause.
- The tests isolate git with `GIT_CONFIG_GLOBAL`, which needs ≥ 2.32. On Ubuntu 20.04 (2.25) or Debian 11 (2.30) the developer's `~/.gitconfig` (`commit.gpgsign`, `core.excludesFile`, `url.*.insteadOf`) leaks into the tests.

**Fix.**
- Once per `sync`, parse `git --version`. Below 2.9: exit 2 with `error: git 2.9 or newer is required for gblog sync (found <v>)`. State the minimum in README and FAQ 38.
- `GIT_ENV`: additionally point `HOME` and `XDG_CONFIG_HOME` at an empty temp directory.

**Tests.** A stub `git` on `PATH` printing `git version 1.8.3.1`: `sync` exits 2 with that message and writes no `sync` record. An existing sync test run with a hostile `~/.gitconfig` in a temp `HOME` still passes.

---

## K7-16 (Low) — interpreter and launcher edges

- **Old `python3`.** `from __future__ import annotations` is a compile-time error on 3.6 (RHEL 8's default `python3`). The user sees a `SyntaxError` traceback from `bin/gblog` or from an installed copy after the system interpreter changed. `install.sh` checks only at install time. *Fix:* in `bin/gblog`, run the same one-line version probe as `install.sh` when `exec` is about to start (about 20 ms, checkout launcher only) and print `gblog needs Python 3.8 or newer (python3 is X.Y)`. For 3.7, add a guard directly after the imports in `gblog.py`.
- **macOS without Command Line Tools.** `/usr/bin/python3` is a stub that exits non-zero after offering an install. `install.sh` reports "python 3.8 or newer is required (found: xcode-select: note: …)". *Fix:* when `uname -s` is Darwin and `python3 -V` fails, print "install the Command Line Tools (`xcode-select --install`) or another Python 3".
- **`bin/gblog` loops forever on a cyclic symlink** (`while [ -L "$src" ]`). *Fix:* a hop counter (40) with a one-line error.
- **`_DAY_FILE_RE = r"^\d{4}-\d{2}-\d{2}\.jsonl$"`.** `\d` matches any Unicode digit and `$` matches before a trailing newline. A file named with Arabic-Indic digits, or `2024-06-01.jsonl\n`, counts as a day file for the layout checks. *Fix:* `r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl\Z"`, as `_DATE_RE` already does. *Test:* such a name gives the `unexpected log file name` warning.
- **Python 3.14** *(to confirm on a 3.14 TTY)*: argparse colours usage and error output on a terminal. `docs/agent-integration.md` describes the lines as plain text. If confirmed, set `color = False` on the parser and each sub-parser, guarded with `hasattr`.

---

## K7-17 — CI matrix gaps

What the workflow does not exercise today, with the smallest addition for each:

| Gap | Why it matters | Addition |
|---|---|---|
| Stock macOS interpreter and shell | Users run `/usr/bin/python3` (3.9.x) and `/bin/bash` 3.2; CI uses setup-python 3.12 and whichever `bash` is first on `PATH` | macOS step: `PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/python3 -m unittest discover -s tests`, after printing both versions |
| One macOS interpreter only | No Python 3.13/3.14 on macOS | add `macos-latest` × `3.13`, with the `-W error::DeprecationWarning` flag |
| `scripts` job is Linux-only | Piped install, CDPATH, relative root, the `shasum` branch and BSD `install`/`grep`/`mktemp` never run | make `scripts` a matrix over `[ubuntu-latest, macos-latest]`; use `shasum -a 256` in the step (present on both); keep the shellcheck step Linux-only |
| Paths with spaces or non-ASCII | `_PLAIN` in `test_scripts.py` *skips* the quoting assertions; every CI temp path is plain | one step: `mkdir -p "$RUNNER_TEMP/gblog tmp é" && TMPDIR="$RUNNER_TEMP/gblog tmp é" python -m unittest discover -s tests`, plus the sourcing tests of K7-03 |
| Real non-UTF-8 file-system encoding | `LC_ALL=C` leaves Python in UTF-8 mode; only stdio is ASCII today | CLI smoke step with `PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 LC_ALL=C` running the K7-06 commands from bash, with non-ASCII `--display`, `--bot`, `--detail` and `--name`; assert the stored text. Do not run the whole suite this way yet: several tests pass non-ASCII argv through `subprocess` and would fail in the parent |
| Case-insensitive assertions | The macOS job runs on APFS, but the only real-FS case test is *skipped* there and the unit test is mocked | the two probe-keyed tests of K7-02 |
| rc lines are never sourced | CI greps for markers; nothing checks that `sh`, `zsh` or `bash` read back the right values | tests of K7-03 and K7-04 |
| Unusable `HOME` | `restricted-env` covers read-only `HOME` only | add `env -u HOME` and `HOME=` cases (K7-07) to that job |
| Legacy-locale reruns of `install.sh` | Idempotence across locales | rerun the "Install from the checkout" step once with `LC_ALL=C` and `cmp` the profile |
| `ubuntu-22.04` pin for 3.8 | The image will be retired during the 0.6.x lifetime | plan a `container: python:3.8` job for the unit tests so the floor does not depend on a runner image |
| Old git | K7-15 | optional container job with an old git, running the sync tests |

Sketch for the first rows:

```yaml
          - os: macos-latest
            python-version: "3.13"
            pyflags: "-W error::DeprecationWarning"
...
      - name: Unit tests with the system interpreter and /bin/bash (macOS)
        if: runner.os == 'macOS'
        run: |
          /usr/bin/python3 --version && /bin/bash --version | head -n 1
          PATH="/usr/bin:/bin:/usr/sbin:/sbin" /usr/bin/python3 -m unittest discover -s tests
      - name: Unit tests under a temp directory with a space and a non-ASCII character
        if: matrix.python-version == '3.12'
        run: |
          mkdir -p "$RUNNER_TEMP/gblog tmp é"
          TMPDIR="$RUNNER_TEMP/gblog tmp é" python -m unittest discover -s tests
```

---

## K7-18 (Low) — no `.gitattributes`

On a Windows checkout with `core.autocrlf=true`:

- `install.sh`, `bin/gblog` and `examples/*.sh` get CRLF and fail under Git-Bash or WSL (`$'\r': command not found`);
- the committed fixture day files change bytes, so `test_committed_fixture_matches_builder` (a byte comparison) fails.

FAQ 34 already limits Windows support, but a contributor's checkout should not break the repository's own tests.

**Fix:** add a `.gitattributes` at the repository root. It does not touch the fixture files.

```
*        text=auto eol=lf
*.sh     text eol=lf
bin/*    text eol=lf
*.jsonl  -text
*.gz     binary
```

---

## Checked, no finding

Recorded so the next reviewer does not repeat the work.

- **bash 3.2 syntax** in `install.sh`, `bin/gblog`, `examples/*.sh` and the embedded hook: `+=` on arrays, `${BASH_SOURCE[0]:-}`, `$'\n'` in `case`, process substitution, `compgen -G`, `${var:0:12}` and `printf %q` all exist in 3.2. Every array expanded under `set -u` is non-empty at that point.
- **BSD userland:** `readlink` without `-f`, `install -m`, `mktemp` and `mktemp -d` without a template, `grep -qxF --`, `grep -oE`, `sed '1s/…//'`, `tail -n 1`, `date -u +%Y-%m-%d` and `mkdir -p --` are used in forms both userlands accept. `sha256sum` is used only behind a `shasum` fallback (the CI *step* is the exception, see K7-17).
- **Python 3.8 floor:** no API newer than 3.8 is used (`str.isascii`, `capture_output`, `reconfigure`, `scandir` as context manager and `os.replace` are all older). Annotations are lazy via `__future__`.
- **Python 3.11–3.13 changes:** every inline `(?i)` is at the start of its pattern (an error elsewhere since 3.11). `re.sub(..., count=1)` uses the keyword (positional is deprecated in 3.13). No `datetime.utcnow()`. `RecursionError` from deep JSON is caught in `_parse_line`, `_read_json` and `_check_record`. The integer-string digit limit (3.11, backported) turns a line with an oversized integer into "not valid JSON" — fail-closed.
- **macOS file API:** `O_NOFOLLOW`, `O_NONBLOCK`, `flock` and directory `fsync` are available; `_is_temp_copy` resolves `/var` → `/private/var` with `realpath` on both sides.
- **C / POSIX locale:** Python is in UTF-8 mode; `_tolerant_streams` and `_write_json` keep stdout valid on ASCII streams; git output is decoded as UTF-8 with replacement; file content is always read and written with an explicit encoding.
- **Read-only `HOME`:** gblog itself writes nothing under `HOME` unless the ledger root or `--rc` is there. The existing `restricted-env` job covers the cases; `HOME` unset or empty is K7-07.

---

## Suggested order of work

1. K7-01 and K7-02: integrity results that are wrong for a platform reason. Both are small and well contained.
2. K7-03, K7-04, K7-07: `install.sh` correctness. One PR, together with the K7-17 sourcing tests.
3. K7-06, K7-08: behaviour that depends on locale or Python patch level.
4. K7-05: needs a maintainer decision on the cost/opt-out trade-off; at minimum the DESIGN §4 wording.
5. K7-09 … K7-16 and K7-18 as capacity allows. The K7-17 jobs should land with the first fix they cover, so each gap is closed by a test and not only by code.