from tradingview_mcp import server


def test_ibkr_chain_tool_delegates_without_order_authority(monkeypatch):
    seen = {}

    def fake_chain(**kwargs):
        seen.update(kwargs)
        return {
            "source": "IBKR",
            "read_only": True,
            "transmit": False,
            "contracts": [],
        }

    monkeypatch.setattr(server, "get_spy_option_chain", fake_chain)

    result = server.ibkr_spy_options_chain(
        expiry="2026-09-01",
        account_mode="live",
        strikes_each_side=3,
        market_data_type="delayed",
    )

    assert seen == {
        "expiry": "2026-09-01",
        "account_mode": "live",
        "strikes_each_side": 3,
        "market_data_type": "delayed",
    }
    assert result["read_only"] is True
    assert result["transmit"] is False


def test_ibkr_order_preview_delegates_to_pure_intent_builder(monkeypatch):
    seen = {}

    def fake_intent(**kwargs):
        seen.update(kwargs)
        return {"intent_id": "sha256:test", "transmit": False}

    monkeypatch.setattr(server, "build_spy_option_order_intent", fake_intent)

    result = server.ibkr_spy_option_order_preview(
        expiry="2026-09-01",
        right="C",
        strike=650,
        limit_price=1.25,
    )

    assert seen == {
        "expiry": "2026-09-01",
        "right": "C",
        "strike": 650,
        "limit_price": 1.25,
        "quantity": 1,
        "action": "BUY",
        "account_mode": "paper",
    }
    assert result == {"intent_id": "sha256:test", "transmit": False}


def test_ibkr_tools_are_explicitly_read_only():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    for name in ("ibkr_spy_options_chain", "ibkr_spy_option_order_preview"):
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
