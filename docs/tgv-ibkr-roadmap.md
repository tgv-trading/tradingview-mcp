# Terminal Gravity IBKR roadmap

This fork adds an IBKR-backed Terminal Gravity lane without turning the MCP server into the broker system of record.

## Frozen first contract

- Instrument: SPY options only.
- Venue and currency: SMART / USD.
- Position shape: one long call or put.
- Order shape: BUY, quantity 1, limit order.
- Market data: explicitly selected Paper or Live IBKR lane through a market-data-only socket.
- Order intent: Paper only, deterministic, content-addressed, non-authoritative, and non-transmittable.
- Live order transmission: disabled.
- Broker truth: only broker-confirmed orders, fills, positions, and protection from the execution/reconciliation services qualify as execution truth.

## Phase 1 — foundation (this change)

1. Add isolated Paper and Live IBKR connection configuration.
2. Read a bounded SPY option chain through `ib_async` without startup account, position, order, or execution synchronization.
3. Expose a deterministic Paper option-order preview with an immutable safety envelope.
4. Reject Live order intent generation and all out-of-policy actions, sizes, and order types.
5. Keep both MCP tools declared read-only.

## Phase 2 — broker evidence

1. Bind quotes to exact contract IDs, request lineage, market-data type, and observation timestamps.
2. Read Paper account, funds, positions, open orders, and executions as separate broker-confirmed snapshots.
3. Add freshness, causal ordering, complete-portfolio, and account-lineage validation.
4. Emit content-addressed broker-state artifacts for the deterministic Risk Gate.

## Phase 3 — Paper execution service

Execution does not belong in the MCP analysis process. Add a separate Terminal Gravity execution service that:

1. accepts only an exact, previously issued, short-lived Paper authorization;
2. atomically consumes that authorization once;
3. submits a broker-native bracket or equivalent protected order;
4. tracks accepted, working, partial, filled, protected, cancelled, and rejected states separately;
5. persists ambiguous timeouts and reconciles them after restart using order IDs, permanent IDs, execution IDs, positions, and open orders;
6. returns broker-confirmed truth to the MCP facade as read-only status.

## Phase 4 — Live readiness, not Live authority

1. Prove Live connectivity and subscribed options data independently from Paper.
2. Keep Live order submission absent or disabled while Paper evidence is incomplete.
3. Require an explicit policy and operator authorization change before any Live execution code can be enabled.
4. Re-run the full adversarial matrix against separate account, gateway, client-ID, ledger, and credential lanes.

## Configuration

Install the optional broker dependency:

```bash
git clone https://github.com/tgv-trading/tradingview-mcp.git
cd tradingview-mcp
pip install '.[ibkr]'
```

Environment variables:

```text
IBKR_HOST=127.0.0.1
IBKR_PAPER_ACCOUNT=<exact Paper account ID>
IBKR_LIVE_ACCOUNT=<exact Live account ID>
IBKR_PAPER_PORT=4002
IBKR_LIVE_PORT=4001
IBKR_PAPER_CLIENT_ID=71
IBKR_LIVE_CLIENT_ID=72
IBKR_REQUEST_TIMEOUT=8
```

Hosts and ports are configurable because TWS and IB Gateway defaults differ. `IBKR_REQUEST_TIMEOUT` must be greater than zero and no more than 15 seconds. Paper and Live must never share an endpoint, client ID, or account ID. Phase 1 verifies the sole broker-managed account against the exact configured lane identity before returning data; it does not read account balances, positions, orders, or executions.

## Current MCP tools

- `ibkr_spy_options_chain`: read-only Paper/Live options-chain data.
- `ibkr_spy_option_order_preview`: pure Paper proposal generation; no broker connection and no order submission.
