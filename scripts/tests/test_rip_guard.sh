#!/usr/bin/env bash
# Tests for guard_rip_in_progress (#495).
#
# Strategy: source the docker.sh helpers, then override check_rip_in_progress
# with a stub so we can exercise the guard's argv-parsing and exit-code
# behaviour without touching docker / postgres.
#
# Run: bash scripts/tests/test_rip_guard.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER_SH="$SCRIPT_DIR/../mkv-lib/docker.sh"

# shellcheck disable=SC1090
source "$DOCKER_SH"

# Override the polling interval so the "default = wait" test doesn't spin
# for a real 10s.
export MKVAUTO_RIP_GUARD_POLL_SECONDS=1

PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf "  ✓ %s\n" "$label"
    PASS=$((PASS + 1))
  else
    printf "  ✗ %s — expected=%q actual=%q\n" "$label" "$expected" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

#
# Case 1: no rip in progress → guard exits 0, prints nothing.
#
echo "Case 1: no rip in progress"
check_rip_in_progress() { return 1; }
out=$(guard_rip_in_progress 2>&1); rc=$?
assert_eq "rc=0 when idle" "0" "$rc"
assert_eq "no output when idle" "" "$out"

#
# Case 2: rip running, default behaviour → blocks then proceeds when rip ends.
#
echo "Case 2: rip running, default (wait)"
# Stub: rip is "running" for the first 2 calls, then clears.
GUARD_TEST_CALLS=0
check_rip_in_progress() {
  GUARD_TEST_CALLS=$((GUARD_TEST_CALLS + 1))
  if [ "$GUARD_TEST_CALLS" -le 2 ]; then
    RIP_GUARD_DETAILS="abc123	/dev/sr0	1	running"
    return 0
  fi
  RIP_GUARD_DETAILS=""
  return 1
}
out=$(guard_rip_in_progress 2>&1); rc=$?
assert_eq "rc=0 once rip clears" "0" "$rc"
if printf '%s' "$out" | grep -q "🎞️"; then
  printf "  ✓ initial wait message printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ initial wait message missing\n  output: %q\n" "$out"; FAIL=$((FAIL + 1))
fi
if printf '%s' "$out" | grep -q "✅ Rip complete"; then
  printf "  ✓ completion message printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ completion message missing\n"; FAIL=$((FAIL + 1))
fi

#
# Case 3: rip running, --force → proceeds immediately.
#
echo "Case 3: rip running, --force"
check_rip_in_progress() {
  RIP_GUARD_DETAILS="abc123	/dev/sr0	1	running"
  return 0
}
out=$(guard_rip_in_progress --force 2>&1); rc=$?
assert_eq "rc=0 with --force" "0" "$rc"
if printf '%s' "$out" | grep -q "⚠️.*proceeding anyway"; then
  printf "  ✓ force warning printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ force warning missing\n"; FAIL=$((FAIL + 1))
fi
if printf '%s' "$out" | grep -q "abc123"; then
  printf "  ✓ active rip details printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ active rip details missing\n"; FAIL=$((FAIL + 1))
fi

#
# Case 4: rip running, --no-wait → fails with explanation.
#
echo "Case 4: rip running, --no-wait"
check_rip_in_progress() {
  RIP_GUARD_DETAILS="def456	/dev/sr1	2	pending"
  return 0
}
out=$(guard_rip_in_progress --no-wait 2>&1); rc=$?
assert_eq "rc=1 with --no-wait" "1" "$rc"
if printf '%s' "$out" | grep -q "refusing to continue"; then
  printf "  ✓ refusal message printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ refusal message missing\n"; FAIL=$((FAIL + 1))
fi
if printf '%s' "$out" | grep -q "def456"; then
  printf "  ✓ active rip details printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ active rip details missing\n"; FAIL=$((FAIL + 1))
fi

#
# Case 5: --force wins over --no-wait when both are passed (force is louder).
#
echo "Case 5: --force + --no-wait → --force wins"
check_rip_in_progress() {
  RIP_GUARD_DETAILS="xyz789	/dev/sr0	1	running"
  return 0
}
out=$(guard_rip_in_progress --force --no-wait 2>&1); rc=$?
assert_eq "rc=0 (force precedence)" "0" "$rc"
if printf '%s' "$out" | grep -q "proceeding anyway"; then
  printf "  ✓ force message printed\n"; PASS=$((PASS + 1))
else
  printf "  ✗ force message missing\n"; FAIL=$((FAIL + 1))
fi

#
# Case 6: target passthrough — guard ignores positional args like "frontend".
#
echo "Case 6: positional args ignored by guard"
check_rip_in_progress() { return 1; }
out=$(guard_rip_in_progress frontend 2>&1); rc=$?
assert_eq "rc=0 with positional arg" "0" "$rc"

echo
echo "─────────────────────────────────────"
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
echo "─────────────────────────────────────"

[ "$FAIL" -eq 0 ]
