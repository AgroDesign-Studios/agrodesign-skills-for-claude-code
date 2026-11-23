#!/usr/bin/env bash
# codex_review.sh — one read-only Codex read of a brief, answered into a file
# (the agrodesign-github-publish skill, Step 3).
#
#   bash codex_review.sh [--root DIR]... [--deny PATH]... <run>/stage3-codex-NN.md
#
# Runs `codex exec` once, from a fresh empty folder, with the brief on stdin, and
# writes Codex's final answer to <run>/stage3-codex-NN.md.codex.md. The event stream
# goes to <answer>.log and the containment report to <answer>.contain. The brief carries
# everything Codex is to read; it is told to open no file.
#
# THE ORDER IS ENFORCED HERE. The brief must be a run's Codex brief, and before anything
# is sent `github_upload_check.py gate --run <run> --before codex` must say GATE=open
# (the staged tree intact, the scan, its triage and Claude's answers for this digest,
# every hit and finding that needs it cleared), and the brief must hold the whole text
# of the indexed bundle-NN.md. Otherwise nothing is sent (exit 11).
#
# MODEL AND EFFORT are requested here, never inherited from ~/.codex/config.toml (which
# is per account and can drift): CODEX_REVIEW_MODEL (default gpt-6-astra, the strongest
# Codex model at the time of writing) and CODEX_REVIEW_EFFORT (default ultra). The
# request alone proves nothing — the server may substitute — so success also requires
# the run's rollout log to show the requested model served it.
#
# BRIEF SCAN. The brief leaves the machine for an outside provider, so it is scanned
# first: gitleaks (AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN or PATH; required) with its default rules
# only — no config, ignore file, allow comment or GITLEAKS_* variable can weaken it —
# and high-precision key and token patterns, and a URL that carries a password. A hit,
# a missing gitleaks or a scanner that fails sends nothing and exits 9; there is no
# switch that skips it.
#
# WHAT CODEX'S COMMANDS CAN READ. Codex runs under the permission profile
# lib/codex_read_deny.py builds, not `--sandbox read-only` (which lets its commands read
# every file this account can read). The profile is read-only and denies the account's
# home (all but the installed Codex package), the other homes, CODEX_HOME, the target of
# each `.env*` at the top of every --root DIR, and each --deny PATH. The commands get
# only the core environment variables (shell_environment_policy "core": HOME, PATH,
# USER and the like). A loaded config that sets `sandbox_mode` makes Codex ignore the
# profile, so after the run the rollout log must record every denied path, or the run is
# refused as `unprotected`. Everything else this account can read stays readable: put
# your own secret folders outside the home in --deny.
#
# CONTAINMENT. `timeout -k` bounds Codex itself, and lib/codex_contain.py kills what a
# command Codex starts inside its sandbox leaves running (an orphaned sandbox process is
# adopted by pid 1 otherwise) and holds the tree to a task and memory budget. Linux only.
#
# CODEX_REVIEW_TIMEOUT: seconds, 1..7200 (default 900; the skill uses 2400 per bundle).
#
# Exit: 0 answer written by the requested model; 2 usage (also a timeout outside
# 1..7200 s, or a brief that is not <run>/stage3-codex-NN.md); 3 codex, timeout, python3, the
# containment helper or the read-deny helper missing, or the deny table could not be
# built (nothing sent); 4 codex failed, timed out or was killed; 5 codex exited 0 but
# wrote no answer; 6 the served model was not the requested one (or could not be read);
# 7 containment: the command's tree broke its budget and was killed, or a process was
# left alive (or the report is missing); 9 the brief looks like it holds a secret,
# nothing sent; 10 the run was not under the read-deny profile; 11 the gate before
# Codex is closed, or the brief does not hold its bundle (nothing sent). On every exit
# but 0 no answer file is left behind, so no later step can count one.
set -uo pipefail

MARK="CODEX_REVIEW"
MODEL="${CODEX_REVIEW_MODEL:-gpt-6-astra}"
EFFORT="${CODEX_REVIEW_EFFORT:-ultra}"
case "$MODEL" in ''|*[!A-Za-z0-9._-]*) echo "CODEX_REVIEW_MODEL must be a plain model id" >&2; exit 2 ;; esac
case "$EFFORT" in ''|*[!a-z]*) echo "CODEX_REVIEW_EFFORT must be a plain effort name" >&2; exit 2 ;; esac

ROOTS=()
DENIES=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) [ "$#" -ge 2 ] || { echo "--root needs a directory" >&2; exit 2; }; ROOTS+=( --root "$2" ); shift 2 ;;
    --deny) [ "$#" -ge 2 ] || { echo "--deny needs a path" >&2; exit 2; }; DENIES+=( --deny "$2" ); shift 2 ;;
    --) shift; break ;;
    -*) echo "usage: codex_review.sh [--root DIR]... [--deny PATH]... <brief-file> [<out-file>]" >&2; exit 2 ;;
    *) break ;;
  esac
done

PROMPT="${1:-}"
if [ -z "$PROMPT" ] || [ ! -r "$PROMPT" ] || [ "$#" -ne 1 ]; then
  echo "usage: codex_review.sh [--root DIR]... [--deny PATH]... <run>/stage3-codex-NN.md" >&2
  exit 2
fi
case "$PROMPT" in /*) ;; *) PROMPT="$PWD/$PROMPT" ;; esac
NAME="$(basename -- "$PROMPT")"
RUN="$(cd "$(dirname -- "$PROMPT")" && pwd)"
case "$NAME" in
  stage3-codex-[0-9]*.md) NN="${NAME#stage3-codex-}"; NN="${NN%.md}" ;;
  *) echo "codex_review.sh: the brief must be a run's <run>/stage3-codex-NN.md (SKILL.md Step 3)" >&2; exit 2 ;;
esac
case "$NN" in ''|*[!0-9]*) echo "codex_review.sh: the brief must be a run's <run>/stage3-codex-NN.md" >&2; exit 2 ;; esac
OUT="$PROMPT.codex.md"
# A previous run's files go now, so a re-run that fails never leaves an old answer
# next to the new brief.
rm -f -- "$OUT" "$OUT.log" "$OUT.contain"
TIMEOUT_S="${CODEX_REVIEW_TIMEOUT:-900}"
case "$TIMEOUT_S" in ''|*[!0-9]*) echo "CODEX_REVIEW_TIMEOUT must be a whole number of seconds" >&2; exit 2 ;; esac
if [ "${#TIMEOUT_S}" -gt 4 ] || [ "$((10#$TIMEOUT_S))" -lt 1 ] || [ "$((10#$TIMEOUT_S))" -gt 7200 ]; then
  echo "CODEX_REVIEW_TIMEOUT must be 1..7200 seconds" >&2
  exit 2
fi

CODEX_BIN="$(command -v codex || true)"
TIMEOUT_BIN="$(command -v timeout || true)"
PY="$(command -v python3 || true)"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
CONTAIN="$LIB/codex_contain.py"
READ_DENY="$LIB/codex_read_deny.py"
CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/github_upload_check.py"
if [ -z "$CODEX_BIN" ] || [ -z "$TIMEOUT_BIN" ] || [ -z "$PY" ] || [ ! -r "$CONTAIN" ] || [ ! -r "$READ_DENY" ] || [ ! -r "$CHECK" ]; then
  echo "$MARK=missing — need codex, coreutils timeout, python3, $CONTAIN, $READ_DENY and $CHECK" >&2
  exit 3
fi

# The order: the gate before Codex, and the indexed bundle's whole text in the brief.
GATE_OUT="$("$PY" -I "$CHECK" gate --run "$RUN" --before codex 2>&1)"; GATE_RC=$?
if [ "$GATE_RC" -ne 0 ]; then
  printf '%s\n' "$GATE_OUT" | head -n 5 >&2
  echo "$MARK=out-of-order — the gate before Codex is closed; nothing was sent" >&2
  exit 11
fi
if ! "$PY" -I - "$RUN" "$NN" "$PROMPT" <<'PY'
import hashlib, json, sys
run, nn, brief = sys.argv[1], sys.argv[2], sys.argv[3]
index = json.load(open(f"{run}/bundles/index.json", encoding="utf-8"))
name = f"bundle-{nn}.md"
data = open(f"{run}/bundles/{name}", "rb").read()
ok = index.get("bundles", {}).get(name) == hashlib.sha256(data).hexdigest() and data in open(brief, "rb").read()
sys.exit(0 if ok else 1)
PY
then
  echo "$MARK=out-of-order — the brief does not hold the whole text of the indexed bundle-$NN.md; nothing was sent" >&2
  exit 11
fi

# The same secret shapes as github_upload_check.py's SECRET_PATTERNS.
SECRET_CONTENT_RE='-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|(^|[^0-9A-Za-z_-])(gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,}|sk-ant-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{32,}|xai-[0-9A-Za-z]{32,}|AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16}|xox[abposr]-[0-9A-Za-z-]{10,}|eyJ[0-9A-Za-z_-]{10,}\.eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{5,})'
URL_PASSWORD_RE='[A-Za-z][A-Za-z0-9+.-]*://[^/:@[:space:]]+:[^/@[:space:]]{3,}@'
BRIEF_HIT=""
GITLEAKS_BIN="${AGRODESIGN_GITHUB_PUBLISH_GITLEAKS_BIN:-$(command -v gitleaks || true)}"
if [ -z "$GITLEAKS_BIN" ] || [ ! -x "$GITLEAKS_BIN" ]; then
  BRIEF_HIT="gitleaks-missing"
else
  # gitleaks' default rules only: an explicit config that only extends the defaults, an
  # empty ignore file, a clean cwd, allow comments ignored, no GITLEAKS_* variable.
  GL="$(mktemp -d "${TMPDIR:-/tmp}/codex-review-gitleaks.XXXXXX")" || { echo "cannot make a temporary folder" >&2; exit 3; }
  printf '[extend]\nuseDefault = true\n' > "$GL/gitleaks.toml"
  mkdir "$GL/ignore" && : > "$GL/ignore/.gitleaksignore"
  ( cd "$GL" && env $(env | sed -n 's/^\(GITLEAKS[^=]*\)=.*/-u \1/p') "$GITLEAKS_BIN" stdin --no-banner --redact \
      --ignore-gitleaks-allow --config "$GL/gitleaks.toml" --gitleaks-ignore-path "$GL/ignore" < "$PROMPT" >/dev/null 2>&1 ) || BRIEF_HIT="gitleaks"
  rm -rf -- "$GL"
fi
if [ -z "$BRIEF_HIT" ]; then
  # grep: 0 = a match, 1 = none, anything else = it could not read or run.
  grep -aEq -e "$SECRET_CONTENT_RE" -- "$PROMPT"; KEY_RC=$?
  grep -aEq -e "$URL_PASSWORD_RE" -- "$PROMPT"; URL_RC=$?
  if [ "$KEY_RC" -eq 0 ]; then
    BRIEF_HIT="key-or-token-pattern"
  elif [ "$URL_RC" -eq 0 ]; then
    BRIEF_HIT="url-with-password"
  elif [ "$KEY_RC" -ne 1 ] || [ "$URL_RC" -ne 1 ]; then
    BRIEF_HIT="scan-failed"
  fi
fi
if [ -n "$BRIEF_HIT" ]; then
  echo "$MARK=secret hit=$BRIEF_HIT — the brief scan found what looks like a secret, or could not run; nothing was sent" >&2
  exit 9
fi

DENY_TABLE="$("$PY" -I "$READ_DENY" table "$CODEX_BIN" "${ROOTS[@]}" "${DENIES[@]}")" || {
  echo "$MARK=missing — the read-deny profile could not be built (lib/codex_read_deny.py said why above); nothing was sent" >&2
  exit 3
}
EMPTY="$(mktemp -d "${TMPDIR:-/tmp}/codex-review-cwd.XXXXXX")" || { echo "cannot make an empty folder" >&2; exit 3; }
trap 'rmdir -- "$EMPTY" 2>/dev/null' EXIT
: > "$OUT" || { echo "cannot write $OUT" >&2; exit 2; }

# --json: the event stream names the thread, whose rollout log records the model that
# served the run and the permission profile it ran under (both checked below).
# --ignore-user-config drops config.toml's plugins and MCP servers; --ignore-rules keeps
# rule files from steering the reviewer. No --sandbox: passing it makes Codex ignore the
# permission profile. --skip-git-repo-check: the empty folder is no repository.
ARGS=( exec --json --ignore-user-config --ignore-rules --skip-git-repo-check
       -c 'default_permissions="upload-read-deny"'
       -c 'permissions.upload-read-deny.extends=":read-only"'
       -c "permissions.upload-read-deny.filesystem=$DENY_TABLE"
       -c 'shell_environment_policy.inherit="core"'
       -C "$EMPTY" -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\""
       --output-last-message "$OUT" )
rc=0
START=$SECONDS
"$PY" -I "$CONTAIN" "$OUT.contain" -- \
  "$TIMEOUT_BIN" -k 30 "$TIMEOUT_S" "$CODEX_BIN" "${ARGS[@]}" < "$PROMPT" > "$OUT.log" 2>&1 || rc=$?
ELAPSED=$((SECONDS - START))
CONTAINED="$(cat -- "$OUT.contain" 2>/dev/null || echo 'report missing')"

# The containment report is judged before rc: a budget kill takes codex down with it,
# and a failed run can leave a process alive or no report at all.
case "$CONTAINED" in
  *"budget=tasks"*|*"budget=memory"*)
    rm -f -- "$OUT"
    echo "$MARK=uncontained contain=[$CONTAINED] — the command's tree broke its budget and was killed (log: $OUT.log)" >&2
    exit 7 ;;
  *"remaining=0 budget=ok"*) ;;
  *)
    rm -f -- "$OUT"
    echo "$MARK=uncontained contain=[$CONTAINED] rc=$rc — a process was left alive, or the containment wrote no report (log: $OUT.log)" >&2
    exit 7 ;;
esac

if [ "$rc" -ne 0 ]; then
  case "$rc" in
    124) WHY="timeout" ;;
    137) if [ "$ELAPSED" -ge "$((10#$TIMEOUT_S))" ]; then WHY="timeout"; else WHY="killed"; fi ;;
    *) WHY="error" ;;
  esac
  rm -f -- "$OUT"
  echo "$MARK=failed why=$WHY rc=$rc requested=$MODEL (log: $OUT.log)" >&2
  tail -n 5 -- "$OUT.log" >&2
  exit 4
fi
if [ ! -s "$OUT" ]; then
  rm -f -- "$OUT"
  echo "$MARK=empty requested=$MODEL (log: $OUT.log)" >&2
  exit 5
fi

SESSIONS="${CODEX_HOME:-${HOME:-}/.codex}/sessions"
# The REQUEST is not the REPORT: read the served model from the rollout log's
# turn_context, and refuse anything else — including "unknown".
SERVED="$("$PY" -I - "$OUT.log" "$SESSIONS" <<'PY'
import glob, json, os, re, sys
thread = None
for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        e = json.loads(raw)
    except ValueError:
        continue
    if isinstance(e, dict) and e.get("type") == "thread.started":
        thread = str(e.get("thread_id") or "")
model = "unknown"
if thread and re.fullmatch(r"[0-9A-Za-z-]+", thread):
    hits = sorted(glob.glob(os.path.join(sys.argv[2], "**", f"rollout-*{thread}*.jsonl"), recursive=True),
                  key=os.path.getmtime, reverse=True)
    for raw in (open(hits[0], encoding="utf-8", errors="replace") if hits else []):
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        p = o.get("payload") if isinstance(o, dict) else None
        if isinstance(p, dict) and o.get("type") == "turn_context" and isinstance(p.get("model"), str):
            model = p["model"]
print(model)
PY
)"
if [ "$SERVED" != "$MODEL" ]; then
  rm -f -- "$OUT"  # no later step may count an answer the requested model did not give
  echo "$MARK=rerouted requested=$MODEL served=${SERVED:-unknown} — the answer was deleted (log: $OUT.log)" >&2
  exit 6
fi
if ! PROFILE_SEEN="$("$PY" -I "$READ_DENY" check "$OUT.log" "$SESSIONS" "$DENY_TABLE")"; then
  rm -f -- "$OUT"
  echo "$MARK=unprotected why=[$PROFILE_SEEN] — the run was not under the read-deny profile, so its commands could read any file this account can read; the answer was deleted (look for a config that sets sandbox_mode) (log: $OUT.log)" >&2
  exit 10
fi

echo "$MARK=ok served=$SERVED effort=$EFFORT contain=[$CONTAINED] out=$OUT"
