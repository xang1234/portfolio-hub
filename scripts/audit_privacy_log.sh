#!/usr/bin/env bash
# Slice 12 HITL — verify the dashboard logs don't leak dollar amounts at
# INFO / WARN levels.
#
# The plan's privacy constraint (issues #10 and #11): every snapshot or fill
# log line at INFO/WARN must NOT include NLV, cash balance, fill price, or
# trade quantity. Dollar amounts (and similar position-size values) belong
# at DEBUG level only — debug logs are typically off in production.
#
# This script greps the running dashboard's container logs for
# digit-looking tokens (1+ digit, optional comma-thousands, optional
# decimal point) inside INFO or WARN lines. It deliberately ignores DEBUG
# (which is allowed to contain anything for development).
#
# Exit code: 0 if clean, 1 if any leaks found, 2 if docker isn't reachable.
#
# Usage:
#   scripts/audit_privacy_log.sh           # default 24h window
#   scripts/audit_privacy_log.sh --since 6h
#   DASHBOARD_SERVICE=otherapp scripts/audit_privacy_log.sh
set -euo pipefail

SERVICE="${DASHBOARD_SERVICE:-dashboard}"
SINCE="${SINCE:-24h}"

# Allow `--since 12h` as a CLI override of the env var.
if [[ "${1:-}" == "--since" && -n "${2:-}" ]]; then
    SINCE="$2"
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker not on PATH — run this on the host where the dashboard is deployed" >&2
    exit 2
fi

# Pull logs. `2>&1` because docker compose writes log lines on stderr.
LOGS="$(docker compose logs --since "$SINCE" --no-color "$SERVICE" 2>&1 || true)"
if [[ -z "$LOGS" ]]; then
    echo "no logs returned for service '$SERVICE' in the last $SINCE — is the container running?" >&2
    exit 2
fi

# Filter to INFO / WARNING / WARN / ERROR lines (everything operator-visible).
# Then grep for dollar-amount-shaped tokens. The pattern matches:
#   - $1,234   $1234.56   $1.50
#   - 1234.56  12,345.67  (bare numbers >= 3 chars or with a decimal)
# Excludes log levels themselves, timestamps, and short integers (qty, attempt
# counters, conIds) that don't look like money.
#
# Allowed-noise patterns (timestamps, conIds, attempt counters, port numbers)
# are stripped first so they don't trigger false positives.
NON_DEBUG="$(echo "$LOGS" | grep -E '\[(INFO|WARNING|WARN|ERROR)' || true)"

if [[ -z "$NON_DEBUG" ]]; then
    # Some log formats use level-as-bare-token without brackets. Try that too.
    NON_DEBUG="$(echo "$LOGS" | grep -E '\b(INFO|WARNING|WARN|ERROR)\b' || true)"
fi

if [[ -z "$NON_DEBUG" ]]; then
    echo "audit: no INFO/WARN/ERROR lines found — nothing to scan."
    exit 0
fi

# Strip the noise patterns before the dollar-amount grep.
SANITIZED="$(echo "$NON_DEBUG" \
    | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+-]+//g'    `# ISO timestamps` \
    | sed -E 's/\[[0-9]+\]//g'                                   `# bracketed integers (pids, line nos)` \
    | sed -E 's/conId=[0-9]+//g'                                 `# conId references` \
    | sed -E 's/clientId=[0-9]+//g'                              `# clientId references` \
    | sed -E 's/attempt [0-9]+//g'                               `# reconnect attempt counters` \
    | sed -E 's/in [0-9]+(\.[0-9]+)?s//g'                        `# "in 5s" / "in 60.0s"` \
    | sed -E 's/[Pp]ort[ =][0-9]+//g'                            `# port numbers` \
    | sed -E 's/account[ _]?id=U[0-9]+//g'                       `# IB account IDs (UXXXXXX)` \
    | sed -E 's/U[0-9]{7,}//g'                                   `# bare UXXXXXXX account IDs` \
    | sed -E 's/HTTP\/[0-9.]+ [0-9]+//g'                         `# HTTP status` \
    | sed -E 's/exec=[A-Za-z0-9.]+//g'                           `# execution IDs` \
)"

# Now look for dollar-amount-looking tokens in what's left.
#   $-prefixed:                       \$[0-9,]+(\.[0-9]+)?
#   bare with decimal:                \b[0-9]{1,3}(,[0-9]{3})*\.[0-9]+\b
#   bare with thousands separator:    \b[0-9]{1,3}(,[0-9]{3})+\b
#   bare integer >= 4 digits:         \b[0-9]{4,}\b   (NLV / balances are typically large)
LEAKS="$(echo "$SANITIZED" | grep -E '(\$[0-9,]+(\.[0-9]+)?|\b[0-9]{1,3}(,[0-9]{3})*\.[0-9]+\b|\b[0-9]{1,3}(,[0-9]{3})+\b|\b[0-9]{4,}\b)' || true)"

if [[ -z "$LEAKS" ]]; then
    echo "audit: no privacy violations found in the last $SINCE of '$SERVICE' INFO/WARN/ERROR logs."
    exit 0
fi

echo "PRIVACY AUDIT FAILED — found dollar-amount-looking tokens in non-DEBUG log lines:" >&2
echo "$LEAKS" | head -50 >&2
echo >&2
echo "Audit which _LOG.info / _LOG.warning call produced these and either:" >&2
echo "  (a) remove the dollar-amount field from the format string, or" >&2
echo "  (b) demote the call to _LOG.debug." >&2
exit 1
