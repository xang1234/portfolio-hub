# IBKR Auth Runbook

## Intended Model

Portfolio Hub runs IB Gateway/TWS in read-only mode for the dashboard. IB Gateway/TWS still needs a daily restart, but the intended auth model is not daily phone approval.

With `READ_ONLY_LOGIN=yes` and IBC Auto Restart, IBKR normally preserves the login token Monday-Saturday. Manual IBKR Mobile authentication is expected after the Saturday/Sunday reset, or earlier if IBKR invalidates the token.

Unexpected manual authentication can happen when IBKR invalidates the token, the Gateway volume is reset, read-only login is disabled, the weekly reset has passed, the Gateway is cold-started outside the preserved-token window, or IBKR requires an additional security check.

## Normal Morning Check

If the dashboard shows IBKR connected, do nothing.

If the dashboard shows reconnecting for less than 2 minutes, wait. The adapter is already reconnecting with backoff and may recover without intervention.

When these auth-resilience controls are deployed, if reconnecting continues, use Retry now in the dashboard to ask the adapter to reconnect immediately.

When Gateway restart controls are deployed, if Retry now does not recover the connection, restart Gateway.

Approve manual IBKR Mobile authentication only if prompted by IBKR, or if a configured Telegram alert says manual auth is likely needed.

## Config

```dotenv
READ_ONLY_LOGIN=yes
READ_ONLY_API=yes
AUTO_RESTART_TIME=06:00 AM
TWS_COLD_RESTART=15:30
RELOGIN_AFTER_TWOFA_TIMEOUT=yes
```

`TWS_COLD_RESTART` maps to IBC's `ColdRestartTime`. IBC applies that field only for the Sunday cold restart, so the value is only the local `HH:MM` time, not a day plus time. With the default `TZ=Asia/Singapore`, `15:30` Sunday is safely after 01:00 US/Eastern in both US daylight saving and standard time.

## Retry Now vs Restart Gateway

When deployed, Retry now tells Portfolio Hub's `IbkrAdapter` to reconnect to the existing Gateway session immediately. Use it when Gateway is probably still running and the dashboard is waiting through reconnect backoff.

When deployed, Restart Gateway restarts the IB Gateway/TWS container/session. Use it when Gateway is unavailable, stuck, or past the point where adapter reconnects are recovering. Restarting Gateway may trigger IBKR Mobile authentication if IBKR no longer accepts the preserved login token.

## Telegram Alerts

Telegram alerts are sent after `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured and the alerting task is deployed.

`reconnecting`: Portfolio Hub detected a connection drop and is trying to reconnect with backoff. Wait briefly unless it persists.

`unavailable >2 minutes`: The connection has not recovered within 2 minutes. When those controls are deployed, try Retry now, then restart Gateway if needed.

`disconnected/backoff exhausted`: Automatic reconnect attempts are exhausted. When restart controls are deployed, restart Gateway and be ready to approve IBKR Mobile if prompted.

`recovered`: Portfolio Hub reconnected successfully. No action is needed.
