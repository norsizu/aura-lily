from __future__ import annotations

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.desserts import DESSERT_CATALOG, apply_dessert_purchase
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.hermes_lily_cli import gateway


def test_dessert_catalog_has_eight_price_tiers_and_cross_axis_tradeoffs():
    assert len(DESSERT_CATALOG) == 8
    assert [item.price for item in DESSERT_CATALOG] == [8, 10, 12, 15, 18, 22, 25, 30]
    assert all(-20 <= item.effects["energy"] <= 10 for item in DESSERT_CATALOG)
    assert all("\n" in item.effect_label[0] for item in DESSERT_CATALOG)
    # A higher price buys a different profile, not every larger number.
    assert DESSERT_CATALOG[0].effects["energy"] > DESSERT_CATALOG[4].effects["energy"]
    assert DESSERT_CATALOG[2].effects["stress"] < DESSERT_CATALOG[4].effects["stress"]
    assert DESSERT_CATALOG[3].effects["satiety"] > DESSERT_CATALOG[4].effects["satiety"]


def test_dessert_purchase_updates_wallet_and_companion_stats():
    state = {"beans": 50, "coins": 50, "mood": 60, "energy": 40, "satiety": 30, "stress": 20}
    updated, result = apply_dessert_purchase(state, "strawberry_cake", device_beans=50)

    assert result["status"] == "purchased"
    assert result["beans"] == 32
    assert updated["beans"] == updated["coins"] == 32
    assert updated["mood"] == 76
    assert updated["energy"] == 40
    assert updated["satiety"] == 43
    assert updated["stress"] == 17
    assert updated["metadata"]["last_dessert_id"] == "strawberry_cake"


def test_dessert_purchase_rejects_insufficient_balance():
    updated, result = apply_dessert_purchase({"beans": 7, "mood": 80}, "pudding")

    assert updated is None
    assert result["status"] == "insufficient_beans"
    assert result["beans"] == 7


def test_gateway_dessert_purchase_persists_state_and_life_event(tmp_path, monkeypatch):
    config = PersonaGatewayConfig(
        enabled=True,
        persona_home=str(tmp_path / "persona"),
        companion_home=str(tmp_path / "companion"),
    )
    store = LilyPersonaStore(config.companion_db_path)
    store.get_or_create_state(config.scope)
    monkeypatch.setattr(gateway, "load_persona_config", lambda: config)
    monkeypatch.setattr(gateway, "LilyPersonaStore", LilyPersonaStore)

    result = gateway.handle_dessert_purchase({"item_id": "pudding", "beans": 50})
    state = store.get_or_create_state(config.scope)

    assert result["ok"] is True
    assert result["beans"] == 42
    assert state["beans"] == 42
    assert store.list_recent_life_events(config.scope, event_prefix="companion.dessert", limit=1)
