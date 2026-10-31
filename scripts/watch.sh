#!/bin/bash
# Generic post-delegation watchdog: full QA in a clean worktree + PR + ONE Telegram line.
# Usage: watch.sh <repo>            (cron entry; repo dir = /data/projects/<repo>)
#
# Armed by launch.sh, which writes /tmp/<repo>-watch.state (REPO/BRANCH/LOG/BASE/PID/REPORTED).
# Silent unless a terminal state is reached for a NEW commit:
#   - claude still running (pgrep -x claude)        -> silent
#   - branch == BASE (origin/main sha at launch)    -> silent
#   - branch sha already in REPORTED_SHA            -> silent (per-commit latch)
#   - otherwise: QA (pytest + ruff check + ruff format --check + placeholder scan) in
#     /data/qa/<repo>/<branch>, open a PR on PASS (if none open), post PASS|FAIL|TIMEOUT.
# Never merges anything. The cron call returns immediately: QA runs in a detached,
# flock-guarded supervisor capped at 30 minutes, so cron script timeouts can't cut it short.
#
# Env overrides (testing): WATCH_STATE (state file), WATCH_QA_ROOT (/data/qa),
# WATCH_TARGET (hermes send target, default telegram), WATCH_TIMEOUT (seconds, 1800),
# WATCH_FOREGROUND=1 (run supervisor inline), WATCH_IGNORE_CLAUDE=1 (skip pgrep check).

MODE="main"
case "$1" in _supervise|_qa) MODE="$1"; shift ;; esac
REPO="$1"
[ -n "$REPO" ] || { echo "usage: watch.sh <repo>" >&2; exit 2; }

SELF="$(readlink -f "$0")"
REPODIR="/data/projects/$REPO"
VENV="$REPODIR/.venv/bin"
STATEF="${WATCH_STATE:-/tmp/$REPO-watch.state}"
LOCKF="$STATEF.lock"
QA_ROOT="${WATCH_QA_ROOT:-/data/qa}"
TARGET="${WATCH_TARGET:-telegram}"
TIMEOUT_S="${WATCH_TIMEOUT:-1800}"
export WATCH_STATE="$STATEF" WATCH_QA_ROOT="$QA_ROOT" WATCH_TARGET="$TARGET"

load_state() {
  [ -f "$STATEF" ] || return 1
  # shellcheck disable=SC1090
  . "$STATEF" || return 1
  [ -n "$BRANCH" ] || return 1
  SHA=$(git -C "$REPODIR" rev-parse --verify -q "refs/heads/$BRANCH^{commit}" 2>/dev/null) || return 1
  [ -n "$SHA" ] && [ "$SHA" != "$BASE" ] || return 1   # not advanced past launch base
  [ "$SHA" != "${REPORTED_SHA:-}" ] || return 1         # this commit already reported
}

latch() {  # latch <sha>: record the reported commit in the state file
  if grep -q '^REPORTED_SHA=' "$STATEF" 2>/dev/null; then
    sed -i "s/^REPORTED_SHA=.*/REPORTED_SHA=$1/" "$STATEF"
  else
    printf 'REPORTED_SHA=%s\n' "$1" >> "$STATEF"
  fi
}

post() {  # post <message>: one message to the chat; stdout fallback if delivery fails
  if ! hermes send -q -t "$TARGET" "$1" >/dev/null 2>&1; then
    printf '%s\n' "$1"
    return 1
  fi
}

# ---------------------------------------------------------------- entry (cron)
if [ "$MODE" = "main" ]; then
  load_state || exit 0
  if [ "${WATCH_IGNORE_CLAUDE:-0}" != "1" ] && pgrep -x claude >/dev/null 2>&1; then exit 0; fi
  # A supervisor already running for this repo holds the lock: stay silent.
  ( flock -n 9 ) 9>"$LOCKF" 2>/dev/null || exit 0
  if [ "${WATCH_FOREGROUND:-0}" = "1" ]; then
    exec bash "$SELF" _supervise "$REPO"
  fi
  setsid nohup bash "$SELF" _supervise "$REPO" >/dev/null 2>&1 < /dev/null &
  exit 0
fi

# ---------------------------------------------------------------- supervisor (lock + 30m cap)
if [ "$MODE" = "_supervise" ]; then
  exec 9>"$LOCKF"
  flock -n 9 || exit 0
  load_state || exit 0   # re-check under the lock (another run may have just latched)
  timeout -k 30 "$TIMEOUT_S" bash "$SELF" _qa "$REPO"
  rc=$?
  if [ "$rc" = "124" ] || [ "$rc" = "137" ]; then
    post "TIMEOUT: $REPO $BRANCH ${SHA:0:7} QA did not finish in $((TIMEOUT_S / 60))m" && latch "$SHA"
  fi
  exit 0
fi

# ---------------------------------------------------------------- QA + PR + report
load_state || exit 0
SHORT="${SHA:0:7}"
SLUG="${BRANCH//\//_}"
WT="$QA_ROOT/$REPO/$BRANCH"
QALOG="$QA_ROOT/$REPO/$SLUG.qa.log"
mkdir -p "$QA_ROOT/$REPO" "$(dirname "$WT")"
: > "$QALOG"

git -C "$REPODIR" fetch -q --prune origin >>"$QALOG" 2>&1 || echo "WARN: git fetch failed; using local refs" >>"$QALOG"

# (Re)create a clean detached worktree at the branch commit. Detached because the branch
# is normally still checked out in $REPODIR, which forbids a second checkout of it.
case "$WT" in "$QA_ROOT/$REPO/"?*) ;; *) echo "refusing worktree path $WT" >&2; exit 1 ;; esac
git -C "$REPODIR" worktree remove --force "$WT" >/dev/null 2>&1
rm -rf -- "$WT"
git -C "$REPODIR" worktree prune
git -C "$REPODIR" worktree add -q --detach "$WT" "$SHA" >>"$QALOG" 2>&1 || {
  post "FAIL | $REPO $BRANCH $SHORT | 0 tests | PR none
could not create QA worktree at $WT (see $QALOG)
next: check the repo/worktree state" && latch "$SHA"
  exit 0
}

# Run the repo's tools from the worktree. The venv's editable install points at $REPODIR/src,
# so put the worktree's src first on PYTHONPATH to test the worktree code, not the live checkout.
export PYTHONPATH="$WT/src:$WT${PYTHONPATH:+:$PYTHONPATH}"
TESTS=(); [ -d "$WT/tests" ] && TESTS=(tests/)   # never collect vendored reference/ trees

# --verbosity=-1 is plain -q regardless of addopts (addopts "-q" + -q = -qq hides the summary).
( cd "$WT" && "$VENV/pytest" --verbosity=-1 -rfE "${TESTS[@]}" ) > "$QALOG.pytest" 2>&1; PYTEST_RC=$?
( cd "$WT" && "$VENV/ruff" check --output-format=concise . ) > "$QALOG.ruff" 2>&1; RUFF_RC=$?
( cd "$WT" && "$VENV/ruff" format --check . ) > "$QALOG.fmt" 2>&1; FMT_RC=$?

# Placeholder scan: added lines of the branch's own diff (since its merge-base with origin/main).
MAINREF=origin/main
git -C "$REPODIR" rev-parse -q --verify "$MAINREF" >/dev/null || MAINREF=main
git -C "$REPODIR" diff -U0 --no-color "$MAINREF...$SHA" > "$QALOG.diff" 2>>"$QALOG"
PY="$VENV/python"; [ -x "$PY" ] || PY=python3
"$PY" -I - "$QALOG.diff" > "$QALOG.scan" 2>&1 <<'PY'
import ipaddress, re, sys

ALLOWED_ACCOUNTS = {"123456789012", "000000000000"}
AKIA = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
ARN = re.compile(r"arn:aws[a-z-]*:[^\s\"'`]*")
ACCT = re.compile(r"(?<![0-9.\-])[0-9]{12}(?![0-9.\-])")
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def ip_is_real(text):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False  # versions like 1.2.300.4 are not addresses
    if any(ip in n for n in DOC_NETS):
        return False
    return ip.is_global


hits, path, lineno = [], "?", 0
for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if raw.startswith("+++ "):
        path = raw[6:].strip() if raw.startswith("+++ b/") else raw[4:].strip()
        continue
    if raw.startswith("@@"):
        m = re.search(r"\+(\d+)", raw)
        lineno = int(m.group(1)) if m else 0
        continue
    if not raw.startswith("+"):
        continue
    line = raw[1:].rstrip("\n")
    found = AKIA.findall(line)
    found += [a for a in ARN.findall(line) if any(x not in ALLOWED_ACCOUNTS for x in re.findall(r"\d{12}", a))]
    found += [a for a in ACCT.findall(line) if a not in ALLOWED_ACCOUNTS]
    found += [ip for ip in IPV4.findall(line) if ip_is_real(ip)]
    for f in dict.fromkeys(found):
        hits.append(f"placeholder: {path}:{lineno}: {f}")
    lineno += 1
print("\n".join(hits))
sys.exit(1 if hits else 0)
PY
SCAN_RC=$?

cat "$QALOG.pytest" "$QALOG.ruff" "$QALOG.fmt" "$QALOG.scan" >> "$QALOG"

# Test count from pytest's final summary line ("3 failed, 309 passed, 2 skipped in 4.1s").
SUMMARY=$(grep -E '[0-9]+ (passed|failed|error)' "$QALOG.pytest" | tail -n 1)
count() { echo "$SUMMARY" | grep -oE "[0-9]+ $1" | grep -oE '^[0-9]+' | head -n 1; }
NPASS=$(count passed); NFAIL=$(count failed); NERR=$(count 'errors?')
NTESTS=$(( ${NPASS:-0} + ${NFAIL:-0} + ${NERR:-0} ))

if [ "$PYTEST_RC" = 0 ] && [ "$RUFF_RC" = 0 ] && [ "$FMT_RC" = 0 ] && [ "$SCAN_RC" = 0 ]; then
  STATUS=PASS
else
  STATUS=FAIL
fi

# ---- PR (PASS only; never merges)
PR="none"
if [ "$STATUS" = PASS ]; then
  cd "$REPODIR" || exit 1
  OPEN=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number // empty' 2>>"$QALOG")
  if [ -n "$OPEN" ]; then
    PR="#$OPEN"
  elif ! git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    PR="none (branch not pushed)"
  else
    # Base: main, unless another remote branch shares a strictly newer merge-base with this
    # commit than origin/main does -> stacked; base the PR on that immediate parent branch.
    BASEBR=main; STACK_NOTE=""
    MB_MAIN=$(git merge-base "$MAINREF" "$SHA" 2>/dev/null)
    best=$(git rev-list --count "$MB_MAIN..$SHA" 2>/dev/null || echo 0)
    if ! git merge-base --is-ancestor "$MAINREF" "$SHA" 2>/dev/null; then
      for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/); do
        cand="${ref#origin/}"
        case "$cand" in HEAD|origin|main|"$BRANCH") continue ;; esac
        mb=$(git merge-base "$ref" "$SHA" 2>/dev/null) || continue
        [ "$mb" = "$SHA" ] && continue                      # cand already contains this commit
        git merge-base --is-ancestor "$MB_MAIN" "$mb" 2>/dev/null || continue
        n=$(git rev-list --count "$mb..$SHA")
        if [ "$n" -lt "$best" ]; then best=$n; BASEBR=$cand; fi
      done
    fi
    if [ "$BASEBR" != main ]; then
      STACK_NOTE="Stacked PR: origin/main is not an ancestor of \`$BRANCH\`; its immediate parent branch (from the merge-base) is \`$BASEBR\`, so this PR targets \`$BASEBR\`. Merge the parent first, then retarget to main."
    fi
    TITLE=$(git log -1 --format=%s "$SHA")
    BODY="Opened automatically by the $REPO watchdog after a clean-worktree QA pass on \`$SHORT\`.

- pytest: ${SUMMARY:-n/a}
- ruff check: clean
- ruff format --check: clean
- placeholder scan (\`$MAINREF...$SHORT\` added lines): clean
${STACK_NOTE:+
$STACK_NOTE
}
🤖 Generated by watch.sh (no agent turn)"
    URL=$(gh pr create --base "$BASEBR" --head "$BRANCH" --title "$TITLE" --body "$BODY" 2>>"$QALOG")
    if [ -n "$URL" ]; then
      PR="#${URL##*/}"; [ "$BASEBR" != main ] && PR="$PR (stacked on $BASEBR)"
    else
      PR="none (gh pr create failed)"
    fi
  fi
fi

# ---- one message
NBAD=$(( ${NFAIL:-0} + ${NERR:-0} ))
TESTS_TXT="$NTESTS tests"; [ "$NBAD" -gt 0 ] && TESTS_TXT="$NTESTS tests ($NBAD failing)"
HEAD_LINE="$STATUS | $REPO $BRANCH $SHORT | $TESTS_TXT | PR $PR"
if [ "$STATUS" = PASS ]; then
  case "$PR" in "#"*) NEXT="next: review + merge PR ${PR%% *}" ;; *) NEXT="next: push the branch / open the PR" ;; esac
  MSG="$HEAD_LINE | $NEXT"
else
  FAILS=$(
    grep -E '^(FAILED|ERROR) ' "$QALOG.pytest"
    [ "$PYTEST_RC" != 0 ] && ! grep -qE '^(FAILED|ERROR) ' "$QALOG.pytest" && tail -n 3 "$QALOG.pytest" | sed 's/^/pytest: /'
    [ "$RUFF_RC" != 0 ] && grep -E '^[^ ].*:[0-9]+:[0-9]+: ' "$QALOG.ruff" | sed 's/^/ruff: /'
    [ "$RUFF_RC" != 0 ] && ! grep -qE ':[0-9]+:[0-9]+: ' "$QALOG.ruff" && tail -n 2 "$QALOG.ruff" | sed 's/^/ruff: /'
    [ "$FMT_RC" != 0 ] && grep -E '^Would reformat' "$QALOG.fmt" | sed 's/^/format: /'
    [ "$SCAN_RC" != 0 ] && cat "$QALOG.scan"
  )
  MSG="$HEAD_LINE
$(printf '%s\n' "$FAILS" | sed '/^$/d' | head -n 10 | cut -c1-200)
next: fix + push to $BRANCH (log: $QALOG)"
fi

post "$MSG" && latch "$SHA"
exit 0
