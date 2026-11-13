"""HTTP REST and WebSocket integration tests for server.py."""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import ENGINE, app

client = TestClient(app)


def test_health_and_static_index():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mode"] == "paper_simulation"
    assert data["live_orders_enabled"] is False

    html = client.get("/")
    assert html.status_code == 200
    assert "ArbitrageX" in html.text


def test_markets_filters_out_not_same_by_default():
    resp = client.get("/api/markets")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["rejected_count"] == 1
    assert all(m["rulesAccepted"] for m in data["markets"])

    resp_all = client.get("/api/markets?include_rejected=true")
    assert resp_all.status_code == 200
    assert len(resp_all.json()["markets"]) == len(data["markets"]) + 1


def test_orderbook_depth_endpoint():
    resp = client.get("/api/orderbook/kalshi:FED-DEC26-CUT25")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["kalshi_yes_asks"]) == 5
    assert len(body["polymarket_no_asks"]) == 5
    assert len(body["slippage_curve"]) >= 5

    missing = client.get("/api/orderbook/kalshi:NONEXISTENT")
    assert missing.status_code == 404


def test_paper_account_and_bot_status_reflect_1000_replay():
    pa = client.get("/api/paper-account")
    assert pa.status_code == 200
    pa_data = pa.json()
    assert pa_data["simulation_only"] is True
    assert pa_data["summary"]["initial_capital"] == 1000.0
    assert pa_data["summary"]["final_equity"] == ENGINE.replay_summary["final_equity"]
    assert len(pa_data["trades"]) == ENGINE.replay_summary["total_trades"]

    st = client.get("/api/bot/status")
    assert st.status_code == 200
    st_data = st.json()
    assert st_data["initial_capital"] == 1000.0
    assert st_data["bankroll"] == ENGINE.replay_summary["final_equity"]
    assert st_data["realized_pnl"] == ENGINE.replay_summary["realized_pnl"]


def test_calculate_endpoint_uses_kalshi_quadratic_rounding():
    resp = client.post(
        "/api/calculate",
        json={
            "capital": 1000,
            "leg1_price": 0.46,
            "leg2_price": 0.48,
            "leg1_venue": "kalshi",
            "leg2_venue": "polymarket",
            "days_to_resolution": 30,
        },
    )
    assert resp.status_code == 200
    d = resp.json()
    assert d["contracts"] > 1000
    assert d["fee1_usd"] > 0
    assert d["fee2_usd"] == 0.0
    assert d["gross_payout"] == float(d["contracts"])
    assert d["net_profit"] == round(d["gross_payout"] - d["total_deployed"], 2)


def test_bot_execute_rejects_not_same_and_closes_valid_position():
    bad = client.post("/api/bot/execute", json={"market_id": "kalshi:TIKTOK-BAN26"})
    assert bad.status_code == 400
    assert "Rule verification failed" in bad.json()["detail"]

    ok = client.post("/api/bot/execute", json={"market_id": "kalshi:FED-DEC26-CUT25", "capital": 300})
    assert ok.status_code == 200
    pos = ok.json()["position"]
    assert pos["contracts"] > 0

    closed = client.post("/api/bot/close", json={"market_id": pos["id"]})
    assert closed.status_code == 200
    assert closed.json()["trade"]["pnl_usd"] == pos["lockedProfit"]


def test_websocket_stream_emits_snapshot():
    with client.websocket_connect("/ws/stream") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert msg["mode"] == "simulated_l2_replay"
        assert len(msg["markets"]) >= 5
        assert msg["summary"]["initial_capital"] == 1000.0
