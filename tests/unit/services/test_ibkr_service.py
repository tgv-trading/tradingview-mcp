from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import pytest

from tradingview_mcp.core.errors import is_error
from tradingview_mcp.core.services.ibkr_service import (
    IbkrConnectionConfig,
    _IbAsyncGateway,
    _first_positive_price,
    _market_data_request_satisfied,
    _normalize_option_ticker,
    _observed_market_data_type,
    _track_market_data_type_callbacks,
    build_spy_option_order_intent,
    get_spy_option_chain,
)


@pytest.fixture(autouse=True)
def configured_accounts(monkeypatch):
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DU_TEST_PAPER")
    monkeypatch.setenv("IBKR_LIVE_ACCOUNT", "U_TEST_LIVE")


class FakeGateway:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict] = []

    def fetch_spy_option_chain(
        self,
        *,
        expiry: str,
        strikes_each_side: int,
        market_data_type: str,
    ) -> dict:
        self.calls.append(
            {
                "expiry": expiry,
                "strikes_each_side": strikes_each_side,
                "market_data_type": market_data_type,
            }
        )
        return {
            "underlying": {"symbol": "SPY", "price": 650.0},
            "contracts": self.rows,
            "market_data_type": market_data_type,
        }


def test_connection_config_keeps_paper_and_live_lanes_separate(monkeypatch):
    monkeypatch.setenv("IBKR_HOST", "ib-gateway.internal")
    monkeypatch.setenv("IBKR_PAPER_PORT", "4002")
    monkeypatch.setenv("IBKR_LIVE_PORT", "4001")
    monkeypatch.setenv("IBKR_PAPER_CLIENT_ID", "41")
    monkeypatch.setenv("IBKR_LIVE_CLIENT_ID", "42")

    paper = IbkrConnectionConfig.from_env("paper")
    live = IbkrConnectionConfig.from_env("live")

    assert paper.host == live.host == "ib-gateway.internal"
    assert paper.account_id == "DU_TEST_PAPER"
    assert live.account_id == "U_TEST_LIVE"
    assert (paper.port, paper.client_id, paper.read_only) == (4002, 41, True)
    assert (live.port, live.client_id, live.read_only) == (4001, 42, True)


@pytest.mark.parametrize("mode", ["", "production", "PAPER "])
def test_connection_config_rejects_ambiguous_account_modes(mode):
    with pytest.raises(ValueError, match="account_mode"):
        IbkrConnectionConfig.from_env(mode)


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "15.01", "1e308"])
def test_connection_config_rejects_unbounded_request_timeouts(monkeypatch, timeout):
    monkeypatch.setenv("IBKR_REQUEST_TIMEOUT", timeout)

    with pytest.raises(ValueError, match="no greater than 15"):
        IbkrConnectionConfig.from_env("paper")


def test_connection_config_requires_exact_selected_account(monkeypatch):
    monkeypatch.delenv("IBKR_PAPER_ACCOUNT")

    with pytest.raises(ValueError, match="IBKR_PAPER_ACCOUNT is required"):
        IbkrConnectionConfig.from_env("paper")


@pytest.mark.parametrize(
    ("paper_port", "live_port", "paper_client", "live_client", "paper_account", "live_account", "message"),
    [
        (4002, 4002, 71, 72, "DU1", "U1", "gateway endpoints"),
        (4002, 4001, 71, 71, "DU1", "U1", "client IDs"),
        (4002, 4001, 71, 72, "DU1", "DU1", "account IDs"),
    ],
)
def test_connection_config_rejects_lane_collisions(
    monkeypatch,
    paper_port,
    live_port,
    paper_client,
    live_client,
    paper_account,
    live_account,
    message,
):
    monkeypatch.setenv("IBKR_PAPER_PORT", str(paper_port))
    monkeypatch.setenv("IBKR_LIVE_PORT", str(live_port))
    monkeypatch.setenv("IBKR_PAPER_CLIENT_ID", str(paper_client))
    monkeypatch.setenv("IBKR_LIVE_CLIENT_ID", str(live_client))
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", paper_account)
    monkeypatch.setenv("IBKR_LIVE_ACCOUNT", live_account)

    with pytest.raises(ValueError, match=message):
        IbkrConnectionConfig.from_env("paper")


def test_get_spy_option_chain_is_read_only_and_bounded():
    gateway = FakeGateway(
        rows=[
            {"conid": 1, "right": "C", "strike": 650.0, "bid": 1.0, "ask": 1.1},
            {"conid": 2, "right": "P", "strike": 650.0, "bid": 0.9, "ask": 1.0},
        ]
    )

    result = get_spy_option_chain(
        expiry="2026-09-01",
        account_mode="paper",
        strikes_each_side=2,
        market_data_type="live",
        gateway_factory=lambda config: gateway,
    )

    assert not is_error(result)
    assert result["source"] == "IBKR"
    assert result["configured_account_mode"] == "paper"
    assert result["broker_account_mode"] is None
    assert result["broker_account_mode_verified"] is False
    assert result["read_only"] is True
    assert result["authoritative"] is False
    assert result["execution_truth"] == "not_execution_truth"
    assert result["contracts"] == gateway.rows
    assert gateway.calls == [
        {
            "expiry": "20260901",
            "strikes_each_side": 2,
            "market_data_type": "live",
        }
    ]


def test_gateway_payload_cannot_override_safety_envelope():
    class HostileGateway(FakeGateway):
        def fetch_spy_option_chain(self, **kwargs):
            return {
                "source": "untrusted",
                "configured_account_mode": "live",
                "broker_account_mode": "live",
                "broker_account_mode_verified": True,
                "read_only": False,
                "authoritative": True,
                "transmit": True,
                "can_execute": True,
                "broker_authority": "broker",
                "execution_truth": "fill",
                "expiry": "20990101",
                "contracts": [],
            }

    result = get_spy_option_chain(
        expiry="2026-09-01",
        account_mode="paper",
        gateway_factory=lambda config: HostileGateway(),
    )

    assert result["source"] == "IBKR"
    assert result["expiry"] == "20260901"
    assert result["configured_account_mode"] == "paper"
    assert result["broker_account_mode"] is None
    assert result["broker_account_mode_verified"] is False
    assert result["read_only"] is True
    assert result["authoritative"] is False
    assert result["transmit"] is False
    assert result["can_execute"] is False
    assert result["broker_authority"] == "none"
    assert result["execution_truth"] == "not_execution_truth"


def test_account_mode_is_verified_only_against_the_configured_exact_account():
    class AttestedGateway(FakeGateway):
        def __init__(self, account_id):
            super().__init__()
            self.account_id = account_id

        def fetch_spy_option_chain(self, **kwargs):
            return {
                "broker_account_id": self.account_id,
                "broker_account_verified": True,
                "contracts": [],
            }

    verified = get_spy_option_chain(
        expiry="2026-09-01",
        account_mode="paper",
        gateway_factory=lambda config: AttestedGateway("DU_TEST_PAPER"),
    )
    mismatch = get_spy_option_chain(
        expiry="2026-09-01",
        account_mode="paper",
        gateway_factory=lambda config: AttestedGateway("U_TEST_LIVE"),
    )

    assert verified["broker_account_mode"] == "paper"
    assert verified["broker_account_mode_verified"] is True
    assert mismatch["broker_account_id"] is None
    assert mismatch["broker_account_mode"] is None
    assert mismatch["broker_account_mode_verified"] is False


def test_get_spy_option_chain_rejects_bad_inputs_without_connecting():
    called = False

    def factory(config):
        nonlocal called
        called = True
        return FakeGateway()

    result = get_spy_option_chain(
        expiry="09/01/2026",
        account_mode="paper",
        strikes_each_side=100,
        market_data_type="live",
        gateway_factory=factory,
    )

    assert is_error(result)
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["read_only"] is True
    assert result["authoritative"] is False
    assert result["transmit"] is False
    assert result["can_execute"] is False
    assert result["broker_authority"] == "none"
    assert result["order_ticket"] is None
    assert result["execution_truth"] == "not_execution_truth"
    assert called is False


def test_ib_async_gateway_uses_readonly_connection_and_disconnects(monkeypatch):
    instances = []

    class Stock:
        secType = "STK"

        def __init__(self, symbol, exchange, currency):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.conId = 0

    class Option:
        secType = "OPT"

        def __init__(
            self,
            symbol,
            expiry,
            strike,
            right,
            exchange,
            multiplier,
            currency,
            tradingClass,
        ):
            self.symbol = symbol
            self.lastTradeDateOrContractMonth = expiry
            self.strike = strike
            self.right = right
            self.exchange = exchange
            self.multiplier = multiplier
            self.currency = currency
            self.tradingClass = tradingClass
            self.conId = 0
            self.localSymbol = ""

    class Ticker:
        def __init__(self, contract, price=1.0):
            self.contract = contract
            self.bid = price
            self.ask = price + 0.1
            self.last = price + 0.05
            self.close = price
            self.bidSize = 10
            self.askSize = 12
            self.volume = 100
            self.modelGreeks = SimpleNamespace(
                impliedVol=0.2,
                delta=0.5,
                gamma=0.01,
                theta=-0.02,
                vega=0.1,
            )
            self.marketDataType = 1
            self._tgv_market_data_type_observed = True

        def marketPrice(self):
            return self.last

    class IB:
        def __init__(self):
            self.connected = False
            self.client_connect_args = None
            self.market_data_type = None
            self.disconnected = False
            self.RequestTimeout = 0
            self.client = SimpleNamespace(connect=self.connect_client)
            instances.append(self)

        def connect(self, *args, **kwargs):
            raise AssertionError("high-level IB.connect must not be used")

        def connect_client(self, host, port, client_id, timeout):
            self.connected = True
            self.client_connect_args = (host, port, client_id, timeout)

        def managedAccounts(self):
            return ["DU_TEST_PAPER"]

        def reqMarketDataType(self, value):
            self.market_data_type = value

        def qualifyContracts(self, *contracts):
            for index, contract in enumerate(contracts, start=1):
                contract.conId = 756733 if contract.secType == "STK" else 9000 + index
                if contract.secType == "OPT":
                    contract.localSymbol = f"SPY {contract.lastTradeDateOrContractMonth} {contract.right}"
            return list(contracts)

        def reqTickers(self, *contracts):
            return [Ticker(contract, 650.0 if contract.secType == "STK" else 1.0) for contract in contracts]

        def reqSecDefOptParams(self, symbol, exchange, sec_type, conid):
            return [
                SimpleNamespace(
                    exchange="SMART",
                    expirations={"20260901"},
                    tradingClass="SPY",
                    multiplier="100",
                    strikes={648.0, 649.0, 650.0, 651.0, 652.0},
                )
            ]

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.disconnected = True
            self.connected = False

    monkeypatch.setitem(
        sys.modules,
        "ib_async",
        SimpleNamespace(IB=IB, Option=Option, Stock=Stock),
    )
    gateway = _IbAsyncGateway(
        IbkrConnectionConfig(
            account_mode="paper",
            account_id="DU_TEST_PAPER",
            host="127.0.0.1",
            port=4002,
            client_id=71,
        )
    )

    result = gateway.fetch_spy_option_chain(
        expiry="20260901",
        strikes_each_side=1,
        market_data_type="live",
    )

    instance = instances[0]
    assert instance.client_connect_args == ("127.0.0.1", 4002, 71, 8.0)
    assert instance.RequestTimeout == 8.0
    assert instance.market_data_type == 1
    assert instance.disconnected is True
    assert result["underlying"]["conid"] == 756733
    assert result["contract_count"] == 6
    assert result["broker_account_verified"] is True
    assert result["observed_market_data_types"] == ["live"]
    assert result["market_data_request_satisfied"] is True
    assert {row["right"] for row in result["contracts"]} == {"C", "P"}


def test_ib_async_gateway_rejects_connected_account_mismatch(monkeypatch):
    instance = None

    class IB:
        def __init__(self):
            nonlocal instance
            instance = self
            self.connected = False
            self.disconnected = False
            self.RequestTimeout = 0
            self.client = SimpleNamespace(connect=self.connect_client)

        def connect_client(self, host, port, client_id, timeout):
            self.connected = True

        def managedAccounts(self):
            return ["U_TEST_LIVE"]

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.disconnected = True
            self.connected = False

    monkeypatch.setitem(
        sys.modules,
        "ib_async",
        SimpleNamespace(IB=IB, Option=object, Stock=object),
    )
    gateway = _IbAsyncGateway(
        IbkrConnectionConfig(
            account_mode="paper",
            account_id="DU_TEST_PAPER",
            host="127.0.0.1",
            port=4002,
            client_id=71,
        )
    )

    with pytest.raises(RuntimeError, match="account lineage"):
        gateway.fetch_spy_option_chain(
            expiry="20260901",
            strikes_each_side=1,
            market_data_type="live",
        )

    assert instance.disconnected is True


def test_ibkr_sentinels_are_not_reported_as_prices_or_sizes():
    contract = SimpleNamespace(
        conId=1,
        localSymbol="SPY TEST",
        lastTradeDateOrContractMonth="20260901",
        right="P",
        strike=650.0,
    )
    ticker = SimpleNamespace(
        contract=contract,
        bid=-1.0,
        ask=1.7976931348623157e308,
        last=-1.0,
        bidSize=-1.0,
        askSize=2147483647,
        volume=0,
        marketDataType=3,
        modelGreeks=SimpleNamespace(
            impliedVol=1.7976931348623157e308,
            delta=-0.75,
            gamma=-2.0,
            theta=-1.0,
            vega=-2.0,
        ),
        _tgv_market_data_type_observed=True,
    )

    result = _normalize_option_ticker(ticker)

    assert result["bid"] is None
    assert result["ask"] is None
    assert result["last"] is None
    assert result["bid_size"] is None
    assert result["ask_size"] is None
    assert result["volume"] == 0
    assert result["implied_volatility"] is None
    assert result["delta"] == -0.75
    assert result["gamma"] is None
    assert result["theta"] == -1.0
    assert result["vega"] is None
    assert result["observed_market_data_type"] == "delayed"
    assert _first_positive_price(-1, 1.7976931348623157e308, 649.5) == 649.5


def test_field_specific_greek_normalization_rejects_unavailable_values():
    contract = SimpleNamespace(
        conId=1,
        localSymbol="SPY TEST",
        lastTradeDateOrContractMonth="20260901",
        right="C",
        strike=650.0,
    )
    ticker = SimpleNamespace(
        contract=contract,
        modelGreeks=SimpleNamespace(
            impliedVol=-1.0,
            delta=2.0,
            gamma=-2.0,
            theta=-2.0,
            vega=-2.0,
        ),
    )

    result = _normalize_option_ticker(ticker)

    assert result["implied_volatility"] is None
    assert result["delta"] is None
    assert result["gamma"] is None
    assert result["theta"] is None
    assert result["vega"] is None


def test_market_data_type_requires_callback_provenance():
    from ib_async import Ticker

    assert _observed_market_data_type(Ticker()) is None

    ticker = SimpleNamespace(marketDataType=1)
    assert _observed_market_data_type(ticker) is None

    wrapper = SimpleNamespace(reqId2Ticker={7: ticker})

    def market_data_type(req_id, value):
        wrapper.reqId2Ticker[req_id].marketDataType = value

    wrapper.marketDataType = market_data_type
    _track_market_data_type_callbacks(SimpleNamespace(wrapper=wrapper))
    wrapper.marketDataType(7, 3)

    assert _observed_market_data_type(ticker) == "delayed"


@pytest.mark.parametrize(
    ("requested", "observed", "satisfied"),
    [
        ("live", ["live", "live"], True),
        ("live", ["live", "delayed"], False),
        ("live", ["frozen"], False),
        ("delayed", ["live", "delayed"], True),
        ("delayed", ["delayed_frozen"], False),
        ("delayed", ["delayed", None], False),
    ],
)
def test_market_data_mode_compares_requested_to_observed(
    requested, observed, satisfied
):
    assert _market_data_request_satisfied(requested, observed) is satisfied


def test_paper_option_order_intent_is_deterministic_and_non_authoritative():
    kwargs = {
        "expiry": "2026-09-01",
        "right": "call",
        "strike": 650.0,
        "limit_price": 1.25,
        "quantity": 1,
        "action": "buy",
        "account_mode": "paper",
    }

    first = build_spy_option_order_intent(**kwargs)
    second = build_spy_option_order_intent(**kwargs)

    assert first == second
    assert first["intent_id"].startswith("sha256:")
    assert first["contract"] == {
        "symbol": "SPY",
        "security_type": "OPT",
        "exchange": "SMART",
        "currency": "USD",
        "expiry": "20260901",
        "right": "C",
        "strike": 650.0,
        "multiplier": "100",
    }
    assert first["order"] == {
        "action": "BUY",
        "order_type": "LMT",
        "quantity": 1,
        "limit_price": 1.25,
    }
    assert first["account_mode"] == "paper"
    assert first["read_only"] is True
    assert first["authoritative"] is False
    assert first["transmit"] is False
    assert first["can_execute"] is False
    assert first["broker_authority"] == "none"
    assert first["order_ticket"] is None
    assert first["execution_truth"] == "not_execution_truth"


def test_live_option_order_intent_fails_closed():
    result = build_spy_option_order_intent(
        expiry=date(2026, 9, 1).isoformat(),
        right="put",
        strike=640,
        limit_price=1.5,
        account_mode="live",
    )

    assert is_error(result)
    assert result["error"]["code"] == "LIVE_EXECUTION_DISABLED"
    assert result["transmit"] is False
    assert result["can_execute"] is False
    assert result["broker_authority"] == "none"
    assert result["execution_truth"] == "not_execution_truth"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("right", "straddle"),
        ("strike", 0),
        ("limit_price", -1),
        ("quantity", 2),
        ("quantity", True),
        ("action", "SELL"),
    ],
)
def test_option_order_intent_rejects_out_of_policy_values(field, value):
    kwargs = {
        "expiry": "2026-09-01",
        "right": "call",
        "strike": 650.0,
        "limit_price": 1.25,
        "quantity": 1,
        "action": "buy",
        "account_mode": "paper",
    }
    kwargs[field] = value

    result = build_spy_option_order_intent(**kwargs)

    assert is_error(result)
    assert result["error"]["code"] == "INVALID_PARAMETER"
