"""IBKR-backed SPY options data and proposal-only Paper order intents.

The module intentionally separates three concerns:

* market-data reads use a read-only TWS/IB Gateway connection;
* order intents are deterministic JSON proposals and never touch a broker;
* broker submission, fills, protection, and reconciliation are out of scope.

Live account market data is allowed when explicitly selected. Live order authority is
not: every order-intent surface fails closed for ``account_mode="live"``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Protocol

from tradingview_mcp.core.errors import ErrorCode, make_error


_ACCOUNT_MODES = {"paper", "live"}
_MARKET_DATA_TYPES = {"live": 1, "delayed": 3}
_OBSERVED_MARKET_DATA_TYPES = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}
_UNSET_DOUBLE = 1.7976931348623157e308
_UNSET_INTEGER = 2147483647
_SAFETY_ENVELOPE = {
    "read_only": True,
    "authoritative": False,
    "transmit": False,
    "can_execute": False,
    "broker_authority": "none",
    "order_ticket": None,
    "execution_truth": "not_execution_truth",
}


@dataclass(frozen=True)
class IbkrConnectionConfig:
    account_mode: str
    account_id: str
    host: str
    port: int
    client_id: int
    read_only: bool = True
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls, account_mode: str) -> "IbkrConnectionConfig":
        if account_mode not in _ACCOUNT_MODES:
            raise ValueError("account_mode must be exactly 'paper' or 'live'")

        lane = account_mode.upper()
        other_lane = "LIVE" if lane == "PAPER" else "PAPER"
        default_port = 4002 if account_mode == "paper" else 4001
        default_client_id = 71 if account_mode == "paper" else 72
        other_default_port = 4001 if account_mode == "paper" else 4002
        other_default_client_id = 72 if account_mode == "paper" else 71
        host = os.environ.get(f"IBKR_{lane}_HOST", os.environ.get("IBKR_HOST", "127.0.0.1"))
        port = _env_int(f"IBKR_{lane}_PORT", default_port)
        client_id = _env_int(f"IBKR_{lane}_CLIENT_ID", default_client_id)
        account_id = _env_required(f"IBKR_{lane}_ACCOUNT")

        other_host = os.environ.get(
            f"IBKR_{other_lane}_HOST", os.environ.get("IBKR_HOST", "127.0.0.1")
        )
        other_port = _env_int(f"IBKR_{other_lane}_PORT", other_default_port)
        other_client_id = _env_int(
            f"IBKR_{other_lane}_CLIENT_ID", other_default_client_id
        )
        other_account = os.environ.get(f"IBKR_{other_lane}_ACCOUNT", "").strip()
        if (host, port) == (other_host, other_port):
            raise ValueError("Paper and Live IBKR lanes must use different gateway endpoints")
        if client_id == other_client_id:
            raise ValueError("Paper and Live IBKR lanes must use different client IDs")
        if other_account and account_id == other_account:
            raise ValueError("Paper and Live IBKR lanes must use different account IDs")

        return cls(
            account_mode=account_mode,
            account_id=account_id,
            host=host,
            port=port,
            client_id=client_id,
            read_only=True,
            timeout_seconds=_env_float("IBKR_REQUEST_TIMEOUT", 8.0),
        )


class IbkrGateway(Protocol):
    def fetch_spy_option_chain(
        self,
        *,
        expiry: str,
        strikes_each_side: int,
        market_data_type: str,
    ) -> dict: ...


GatewayFactory = Callable[[IbkrConnectionConfig], IbkrGateway]


def _env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _env_float(name: str, default: float, maximum: float = 15.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = float(raw)
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive finite number no greater than {maximum:g}")
    return value


def _normalize_expiry(expiry: str) -> str:
    if not isinstance(expiry, str):
        raise ValueError("expiry must be an ISO date string (YYYY-MM-DD)")
    parsed = date.fromisoformat(expiry)
    if parsed.isoformat() != expiry:
        raise ValueError("expiry must be an ISO date string (YYYY-MM-DD)")
    return parsed.strftime("%Y%m%d")


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if abs(numeric) >= _UNSET_DOUBLE or numeric == _UNSET_INTEGER:
        return None
    return numeric


def _positive_price(value) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric > 0 else None


def _nonnegative_number(value) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric >= 0 else None


def _delta(value) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and -1 <= numeric <= 1 else None


def _theta(value) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric != -2 else None


def _track_market_data_type_callbacks(ib) -> None:
    wrapper = getattr(ib, "wrapper", None)
    original = getattr(wrapper, "marketDataType", None)
    req_id_to_ticker = getattr(wrapper, "reqId2Ticker", None)
    if not callable(original) or not hasattr(req_id_to_ticker, "get"):
        return

    def tracked_market_data_type(req_id, market_data_type):
        result = original(req_id, market_data_type)
        ticker = req_id_to_ticker.get(req_id)
        if ticker is not None:
            setattr(ticker, "_tgv_market_data_type_observed", True)
        return result

    wrapper.marketDataType = tracked_market_data_type


def _gateway_error(exc: Exception, *, context: str) -> dict:
    if isinstance(exc, ImportError):
        return _make_safe_error(
            ErrorCode.DEPENDENCY_MISSING,
            "IBKR support requires the TGV fork's optional dependency: install with pip install '.[ibkr]'",
            context=context,
            retryable=False,
        )
    return _make_safe_error(
        ErrorCode.UPSTREAM_ERROR,
        f"{context} failed: {type(exc).__name__}: {exc}",
        source="IBKR",
        retryable=True,
    )


def _make_safe_error(code: ErrorCode | str, message: str, **context) -> dict:
    return {**make_error(code, message, **context), **_SAFETY_ENVELOPE}


def get_spy_option_chain(
    expiry: str,
    account_mode: str = "paper",
    strikes_each_side: int = 5,
    market_data_type: str = "live",
    *,
    gateway_factory: GatewayFactory | None = None,
) -> dict:
    """Return a bounded SPY option chain from an IBKR Paper or Live data lane.

    Both lanes use a market-data-only socket and verify the exact configured
    account identity. ``account_mode`` chooses the IBKR gateway/account lane;
    ``market_data_type`` independently selects real-time or delayed data.
    """
    try:
        normalized_expiry = _normalize_expiry(expiry)
        if account_mode not in _ACCOUNT_MODES:
            raise ValueError("account_mode must be exactly 'paper' or 'live'")
        if isinstance(strikes_each_side, bool) or not isinstance(strikes_each_side, int):
            raise ValueError("strikes_each_side must be an integer")
        if not 1 <= strikes_each_side <= 10:
            raise ValueError("strikes_each_side must be between 1 and 10")
        if market_data_type not in _MARKET_DATA_TYPES:
            raise ValueError("market_data_type must be exactly 'live' or 'delayed'")
        config = IbkrConnectionConfig.from_env(account_mode)
    except (TypeError, ValueError) as exc:
        return _make_safe_error(ErrorCode.INVALID_PARAMETER, str(exc))

    factory = gateway_factory or _IbAsyncGateway
    try:
        payload = factory(config).fetch_spy_option_chain(
            expiry=normalized_expiry,
            strikes_each_side=strikes_each_side,
            market_data_type=market_data_type,
        )
    except Exception as exc:
        return _gateway_error(exc, context="IBKR SPY option-chain request")

    verified_account = payload.get("broker_account_id")
    account_verified = (
        payload.get("broker_account_verified") is True
        and verified_account == config.account_id
    )
    return {
        **payload,
        "source": "IBKR",
        "expiry": normalized_expiry,
        "configured_account_mode": account_mode,
        "broker_account_id": verified_account if account_verified else None,
        "broker_account_verified": account_verified,
        "broker_account_mode": account_mode if account_verified else None,
        "broker_account_mode_verified": account_verified,
        **_SAFETY_ENVELOPE,
    }


def build_spy_option_order_intent(
    expiry: str,
    right: str,
    strike: float,
    limit_price: float,
    quantity: int = 1,
    action: str = "BUY",
    account_mode: str = "paper",
) -> dict:
    """Build a deterministic, non-transmittable SPY option order proposal.

    The frozen first policy lane is one long SPY option contract using a limit
    order. The result is content-addressed for auditability but carries no broker
    authority and is never sent to TWS/IB Gateway.
    """
    if account_mode == "live":
        return _make_safe_error(
            ErrorCode.LIVE_EXECUTION_DISABLED,
            "Live IBKR order authority is disabled; only Paper proposal generation is available",
            account_mode="live",
        )

    try:
        if account_mode != "paper":
            raise ValueError("account_mode must be exactly 'paper' or 'live'")
        normalized_expiry = _normalize_expiry(expiry)
        normalized_right = right.upper()
        if normalized_right in {"CALL", "C"}:
            normalized_right = "C"
        elif normalized_right in {"PUT", "P"}:
            normalized_right = "P"
        else:
            raise ValueError("right must be CALL/C or PUT/P")
        normalized_action = action.upper()
        if normalized_action != "BUY":
            raise ValueError("the Paper-first policy permits BUY orders only")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity != 1:
            raise ValueError("the Paper-first policy requires quantity=1")
        normalized_strike = _required_positive_number(strike, "strike")
        normalized_limit = _required_positive_number(limit_price, "limit_price")
    except (AttributeError, TypeError, ValueError) as exc:
        return _make_safe_error(
            ErrorCode.INVALID_PARAMETER,
            str(exc),
            account_mode=account_mode,
        )

    proposal = {
        "schema_version": "tgv.ibkr.option-order-intent.v1",
        "account_mode": "paper",
        "read_only": True,
        "authoritative": False,
        "transmit": False,
        "can_execute": False,
        "broker_authority": "none",
        "order_ticket": None,
        "execution_truth": "not_execution_truth",
        "contract": {
            "symbol": "SPY",
            "security_type": "OPT",
            "exchange": "SMART",
            "currency": "USD",
            "expiry": normalized_expiry,
            "right": normalized_right,
            "strike": normalized_strike,
            "multiplier": "100",
        },
        "order": {
            "action": "BUY",
            "order_type": "LMT",
            "quantity": 1,
            "limit_price": normalized_limit,
        },
    }
    canonical = json.dumps(proposal, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"intent_id": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}", **proposal}


def _required_positive_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return normalized


class _IbAsyncGateway:
    """Thin, read-only adapter around the optional ``ib_async`` dependency."""

    def __init__(self, config: IbkrConnectionConfig) -> None:
        self.config = config

    def fetch_spy_option_chain(
        self,
        *,
        expiry: str,
        strikes_each_side: int,
        market_data_type: str,
    ) -> dict:
        try:
            from ib_async import IB, Option, Stock
        except ImportError as exc:
            raise ImportError("ib_async is not installed") from exc

        ib = IB()
        try:
            # IB.connect() performs startup account/position/order synchronization.
            # Phase 1 opens only the low-level socket used for market-data requests.
            ib.RequestTimeout = self.config.timeout_seconds
            _track_market_data_type_callbacks(ib)
            ib.client.connect(
                self.config.host,
                self.config.port,
                self.config.client_id,
                self.config.timeout_seconds,
            )
            managed_accounts = list(ib.managedAccounts())
            if managed_accounts != [self.config.account_id]:
                raise RuntimeError(
                    "IBKR connected account lineage does not exactly match the configured lane"
                )
            ib.reqMarketDataType(_MARKET_DATA_TYPES[market_data_type])

            underlying = Stock("SPY", "SMART", "USD")
            qualified_underlying = ib.qualifyContracts(underlying)
            if not qualified_underlying:
                raise RuntimeError("IBKR did not qualify the SPY underlying contract")
            underlying = qualified_underlying[0]

            underlying_tickers = ib.reqTickers(underlying)
            if not underlying_tickers:
                raise RuntimeError("IBKR returned no SPY underlying quote")
            underlying_ticker = underlying_tickers[0]
            spot = _first_positive_price(
                underlying_ticker.marketPrice(),
                getattr(underlying_ticker, "last", None),
                getattr(underlying_ticker, "close", None),
            )
            if spot is None:
                raise RuntimeError("IBKR returned no usable SPY underlying price")

            chains = ib.reqSecDefOptParams("SPY", "", "STK", underlying.conId)
            chain = _select_spy_chain(chains, expiry)
            strikes = _bounded_strikes(chain.strikes, spot, strikes_each_side)
            contracts = [
                Option(
                    "SPY",
                    expiry,
                    strike,
                    right,
                    "SMART",
                    multiplier=str(chain.multiplier or "100"),
                    currency="USD",
                    tradingClass=chain.tradingClass,
                )
                for strike in strikes
                for right in ("C", "P")
            ]
            qualified_options = ib.qualifyContracts(*contracts)
            if len(qualified_options) != len(contracts):
                raise RuntimeError("IBKR did not qualify the complete bounded option set")
            tickers = ib.reqTickers(*qualified_options)
            if len(tickers) != len(qualified_options):
                raise RuntimeError("IBKR returned an incomplete bounded option snapshot")

            normalized_contracts = [_normalize_option_ticker(ticker) for ticker in tickers]
            observed_underlying_type = _observed_market_data_type(underlying_ticker)
            observed_values = [observed_underlying_type]
            observed_values.extend(
                item["observed_market_data_type"] for item in normalized_contracts
            )
            observed_types = sorted({value for value in observed_values if value is not None})
            return {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "requested_market_data_type": market_data_type,
                "observed_market_data_types": observed_types,
                "market_data_request_satisfied": _market_data_request_satisfied(
                    market_data_type, observed_values
                ),
                "broker_account_id": self.config.account_id,
                "broker_account_verified": True,
                "request_timeout_seconds": self.config.timeout_seconds,
                "underlying": {
                    "symbol": "SPY",
                    "conid": underlying.conId,
                    "price": spot,
                    "observed_market_data_type": observed_underlying_type,
                },
                "contract_count": len(tickers),
                "contracts": normalized_contracts,
            }
        finally:
            if ib.isConnected():
                ib.disconnect()


def _first_positive_price(*values) -> float | None:
    for value in values:
        normalized = _positive_price(value)
        if normalized is not None:
            return normalized
    return None


def _select_spy_chain(chains, expiry: str):
    candidates = [
        chain
        for chain in chains
        if expiry in chain.expirations and chain.tradingClass == "SPY"
    ]
    if not candidates:
        raise ValueError(f"IBKR returned no SPY option chain for expiry {expiry}")
    return next((chain for chain in candidates if chain.exchange == "SMART"), candidates[0])


def _bounded_strikes(strikes, spot: float, strikes_each_side: int) -> list[float]:
    valid = sorted(
        float(strike)
        for strike in strikes
        if _finite_number(strike) is not None and float(strike) > 0
    )
    below = [strike for strike in valid if strike < spot][-strikes_each_side:]
    at_or_above = [strike for strike in valid if strike >= spot][: strikes_each_side + 1]
    return below + at_or_above


def _normalize_option_ticker(ticker) -> dict:
    contract = ticker.contract
    greeks = getattr(ticker, "modelGreeks", None)
    return {
        "conid": contract.conId,
        "local_symbol": contract.localSymbol,
        "expiry": contract.lastTradeDateOrContractMonth,
        "right": contract.right,
        "strike": _positive_price(contract.strike),
        "bid": _positive_price(getattr(ticker, "bid", None)),
        "ask": _positive_price(getattr(ticker, "ask", None)),
        "last": _positive_price(getattr(ticker, "last", None)),
        "bid_size": _nonnegative_number(getattr(ticker, "bidSize", None)),
        "ask_size": _nonnegative_number(getattr(ticker, "askSize", None)),
        "volume": _nonnegative_number(getattr(ticker, "volume", None)),
        "implied_volatility": _nonnegative_number(getattr(greeks, "impliedVol", None)),
        "delta": _delta(getattr(greeks, "delta", None)),
        "gamma": _nonnegative_number(getattr(greeks, "gamma", None)),
        "theta": _theta(getattr(greeks, "theta", None)),
        "vega": _nonnegative_number(getattr(greeks, "vega", None)),
        "observed_market_data_type": _observed_market_data_type(ticker),
    }


def _observed_market_data_type(ticker) -> str | None:
    if getattr(ticker, "_tgv_market_data_type_observed", False) is not True:
        return None
    value = getattr(ticker, "marketDataType", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return _OBSERVED_MARKET_DATA_TYPES.get(value)


def _market_data_request_satisfied(
    requested: str, observed_values: list[str | None]
) -> bool:
    if not observed_values or any(value is None for value in observed_values):
        return False
    accepted = {"live"} if requested == "live" else {"live", "delayed"}
    return set(observed_values).issubset(accepted)
