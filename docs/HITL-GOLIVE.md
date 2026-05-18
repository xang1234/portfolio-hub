# HITL Live-Trading Readiness Review

This is the operator runbook for [Issue #12](https://github.com/xang1234/portfolio-hub/issues/12) —
the go/no-go gate before the dashboard becomes the primary daily portfolio view.

**No new dashboard code is involved.** This document drives a manual end-to-end
verification of the running system against a live IBKR account on the spare
laptop. Every check has a command (or set of commands) and an explicit
pass/fail criterion.

Stop and file a regression issue the moment any check fails — do not paper over
red and continue.

---

## Pre-flight

Before starting:

```sh
cd ~/portfolio-hub   # or wherever you deployed
docker compose ps
```

Confirm:
- `portfolio-hub-ib-gateway` is `Up` (running) AND
- `portfolio-hub-dashboard` is `Up` (running).

If either is `Exit` or `Restarting`, fix that before opening the runbook —
nothing below will produce meaningful signal.

---

## 1. Security boundary (Tailscale)

### 1.1 Phone reaches the dashboard on Tailscale

On the phone, with Tailscale **on**, in any browser:

```
http://<laptop-tailscale-hostname>:8080
```

**Pass:** the index page loads with positions visible within ~2 seconds.
**Fail:** record the error (timeout? DNS? TLS?) and check the Tailnet ACLs
allow the phone → laptop port-8080 path.

### 1.2 Dashboard is unreachable WITHOUT Tailscale

On the phone, turn Tailscale **off** (or switch to cellular data with no VPN),
then in the browser:

```
http://<laptop-tailscale-hostname>:8080
http://<laptop-WAN-IP>:8080
```

**Pass:** both URLs fail to load (timeout or "site not reachable").
**Fail:** if either loads, the laptop is exposing 8080 publicly — **stop the
runbook** and fix before proceeding.

### 1.3 Tailscale Funnel is disabled

On the laptop:

```sh
tailscale serve status
tailscale funnel status
```

**Pass:** both report no active serve/funnel configurations, OR `serve` shows
only the dashboard on the Tailnet-only path (not public).
**Fail:** any "public" or "funnel: on" output → disable immediately:

```sh
tailscale funnel --https=443 off
tailscale serve --https=443 off
```

### 1.4 Port 8080 is not publicly reachable

From a machine on a totally different network (e.g., a friend's phone, a VPS):

```sh
nmap -p 8080 -Pn <laptop-public-IP>
```

**Pass:** `8080/tcp filtered` or `closed`.
**Fail:** `8080/tcp open` → router is port-forwarding; remove the forward.

---

## 2. Read-only API enforcement

### 2.1 Effective READ_ONLY_API=yes inside the gateway container

```sh
docker compose exec ib-gateway env | grep READ_ONLY_API
```

**Pass:** prints `READ_ONLY_API=yes`.
**Fail:** any other value → fix `.env` and `docker compose up -d` before the
next step. The next step places a real order; doing it without READ_ONLY_API
risks an actual trade.

### 2.2 Gateway rejects order placement with read-only error

Run the standalone verification script:

```sh
.venv/bin/python scripts/verify_read_only_api.py
```

The script connects to the gateway (NOT through the dashboard), submits a tiny
limit far below market on a US ETF, asserts the response references read-only
mode, then disconnects.

**Pass:** script exits 0 with the message `READ-ONLY VERIFIED`.
**Fail:** script exits non-zero or the order gets a real status (Submitted,
PreSubmitted, Filled). **Stop the runbook** and audit the gateway config.

**After verification, the script is safe to keep** — it doesn't modify
anything and is useful to re-run after a gateway upgrade. Per the original
issue text you may also delete it (`rm scripts/verify_read_only_api.py`).

---

## 3. Data correctness vs IB TWS

Open IB TWS or IB Mobile alongside the dashboard.

### 3.1 Three random positions agree on market value

Pick three positions: one HKEX, one US, one CASH balance. For each:

| Field | TWS source | Dashboard source | Tolerance |
|---|---|---|---|
| Last price | TWS quote panel | Dashboard "Last" column | exact match within 1s |
| Quantity | TWS positions panel | Dashboard "Qty" column | exact |
| MV (USD) | TWS account window → "Stock Market Value" or per-row USD | Dashboard "MV USD" column | within **1%** |

**Pass:** all three within tolerance.
**Fail:** open a regression issue with the position, the TWS value, the
dashboard value, and the FX rate the dashboard is using (visible in row
detail modal — long-press the row).

### 3.2 Net Liquidation Value per linked account

For each account:

```sh
.venv/bin/python scripts/dump_seeds.py --table account-summary
```

Compare against TWS account window. **Pass:** within **0.5%**.

### 3.3 Dual-listed positions are separate rows

If you hold (e.g.) `9988.HK` AND `BABA.US`, both should appear as separate
rows on the dashboard. **Pass:** two rows with different `canonical_symbol`
values. **Fail:** they're merged into one row → file a regression.

---

## 4. Market hours panel correctness

The market drawer at the top of the page shows one card per held exchange.

### 4.1 HKEX during regular trading hours

Between **09:30 and 16:00 HKT** (excluding lunch):

**Pass:** HKEX card shows `🟢 OPEN` and "Closes at 16:00 HKT · in Xh Ym".

### 4.2 HKEX during lunch break

Between **12:00 and 13:00 HKT**:

**Pass:** HKEX card shows `🟡 LUNCH` and "Reopens at 13:00 HKT".

### 4.3 HKEX outside hours

After 16:00 HKT on a weekday, or any weekend hour:

**Pass:** HKEX card shows `🔴 CLOSED` and "Opens at 09:30 HKT [next session]".

### 4.4 NYSE extended hours

US pre-market (04:00–09:30 ET) or post-market (16:00–20:00 ET):

**Pass:** NYSE card shows `🌒 EXTENDED` with subtext "Pre-market until 09:30 ET"
or "After-hours until 20:00 ET".

---

## 5. Resilience (reconnect + mobile background)

### 5.1 Gateway stop → reconnecting badge → stale rows

On the laptop:

```sh
docker compose stop ib-gateway
```

Within 5 seconds, on the dashboard:
- Badge flips to `🟡 IBKR reconnecting (5s)` then `(15s)` then `(60s)`.
- All STK rows go to **~55% opacity** with `⚠️` next to last_price.
- CASH rows stay at full opacity (no ⚠️ — they don't "tick").

### 5.2 Gateway start → reconnect → live ticks

```sh
docker compose start ib-gateway
```

Within ~10 seconds:
- Badge flips back to `🟢 IBKR connected`.
- Opacity returns to 100%, ⚠️ disappears.
- Prices resume ticking (watch one position; it should change within a minute
  during market hours).

### 5.3 Daily restart cycle (next-day check)

The IBKR Gateway automatically restarts ~midnight in the configured timezone.
The morning after:

```sh
docker compose logs dashboard --since 12h | grep -E "RECONNECTING|Reconnect attempt"
```

**Pass:** the log shows `RECONNECTING` followed by `Reconnect attempt N succeeded`,
all without operator intervention.
**Fail:** if the dashboard is stuck `🔴 disconnected`, file a regression with the
log slice.

### 5.4 Phone background → foreground → full snapshot

1. Open the dashboard on the phone.
2. Background the browser app (home button).
3. Wait 10+ minutes (long enough to exceed iOS Safari's SSE timeout).
4. Foreground the browser.

**Pass:** within 5 seconds, all rows are visible with fresh prices (full
snapshot arrived via SSE reconnect), and ticking resumes.
**Fail:** stale prices for >30 seconds, or visible "spinner of doom" → file
a regression citing iOS Safari SSE timeout handling.

---

## 6. Privacy / logging

### 6.1 No dollar amounts at INFO/WARN

```sh
scripts/audit_privacy_log.sh
```

The script greps the last 24h of dashboard logs for digit-pattern tokens
that look like dollar amounts in non-DEBUG levels.

**Pass:** script exits 0 with `no privacy violations found`.
**Fail:** lists offending log lines → audit which `_LOG.info(...)` call
includes a dollar field and fix it.

### 6.2 No secret material in logs

```sh
docker compose logs dashboard --since 24h | grep -iE "TWS_USERID|TWS_PASSWORD|IB_PASS"
```

**Pass:** no output. **Fail:** any output → the env-var leak is a Critical
bug. File regression and rotate the IB password.

---

## 7. Seed jobs working

### 7.1 equity_snapshots has rows after first market close

Wait until at least one held-exchange close has elapsed since the dashboard
started, then:

```sh
.venv/bin/python scripts/dump_seeds.py --table equity_snapshots --since 24h
```

**Pass:** at least one row per linked account per exchange close. Verify
`snapshot_session` matches the expected exchange (`NYSE_CLOSE` if you waited
through 16:00 ET, `HKEX_CLOSE` if you waited through 16:00 HKT, etc.).
**Fail:** no rows → check `docker compose logs dashboard | grep snapshot`
for the scheduled-fire log lines.

### 7.2 fills captures a deliberate small live trade

This step requires placing a real trade. **Skip if you don't want to.**

1. Note the time.
2. Place a small market order via IB Mobile (e.g., 1 share of a low-priced
   ETF — total cost < $20 + commission).
3. Wait 30 seconds.
4. ```sh
   .venv/bin/python scripts/dump_seeds.py --table fills --since 1h
   ```

**Pass:** the new fill appears in the table with correct `side` (BUY/SELL),
`quantity`, `price`, `currency`, and `filled_at` matching the IB confirmation.
**Fail:** missing row → check `docker compose logs dashboard | grep -i "captured fill"`.
If absent, the execDetailsEvent didn't fire — run the reconcile:

```sh
curl -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN:-}" \
  http://<laptop-hostname>:8080/admin/reconcile-fills
```

If the reconcile inserts the missing fill, the live stream had a hiccup
(file a regression for slice 11 to look at). If reconcile also returns 0,
the gateway's `reqExecutionsAsync` isn't returning the fill — escalate.

---

## 8. Mobile UX final pass

On the actual phone you'll use daily:

### 8.1 Portrait fits 5 columns without scroll
**Pass:** Holding name (with flag), Qty, Last, MV native, MV USD all visible
end-to-end without horizontal scroll.

### 8.2 Touch targets
**Pass:** every chip (broker, account, asset filter), sort header, and the
row long-press detail trigger respond on first tap; no mis-fires.

### 8.3 Dark mode in both system themes
Toggle device dark mode on/off (Settings → Display & Brightness).
**Pass:** the page re-renders with correct contrast in both modes (gain/loss
colors still distinguishable; market badges still legible).

### 8.4 Pull-to-refresh
At the top of the holdings list, swipe down until the spinner appears, then
release.
**Pass:** the page reloads, SSE reconnects, and a fresh full snapshot arrives.

---

## Decision

After every check above passes:

✅ **Declare the dashboard production-ready for daily use.**

Document any minor gaps as v1.1 follow-up issues. Pin this runbook URL in
the project README's "operations" section so the next contributor can re-run
it after non-trivial changes.

If any check fails: file a regression issue per failure, link it to issue #12,
and block the go-live until resolved.
