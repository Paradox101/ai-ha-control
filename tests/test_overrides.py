"""Only what a resident really changed keeps the agent away, and only until it is back as it was (0.6.24)."""

from app.ha.context import ContextTracker
from app.watcher.rules import Watcher

from .test_security_comfort import act_everything, event

RESIDENT_CTX = {"id": "u1", "user_id": "vincent"}


def airco(ctx):
    ctx.index.update_state({"entity_id": "climate.slaapkamer_airco", "state": "off",
                            "attributes": {"friendly_name": "Slaapkamer Airco", "temperature": 20}})


async def test_override_ends_when_the_airco_is_off_again(ctx, notifier):
    await act_everything(ctx)
    ctx.contexts = ContextTracker()
    airco(ctx)
    watcher = Watcher(ctx, notifier)
    on = event(ctx.index, "climate.slaapkamer_airco", "off", "cool", context=RESIDENT_CTX, temperature=20)
    on["old_state"]["attributes"] = {"temperature": 20}
    await watcher.handle_state_changed(on)
    assert "climate.slaapkamer_airco" in ctx.db.active_overrides()
    off = event(ctx.index, "climate.slaapkamer_airco", "cool", "off", context=RESIDENT_CTX, temperature=20)
    await watcher.handle_state_changed(off)
    assert ctx.db.active_overrides() == {}


async def test_integrations_and_cars_are_not_manual_changes(ctx, notifier):
    await act_everything(ctx)
    ctx.contexts = ContextTracker()
    ctx.index.update_state({"entity_id": "media_player.spotify_vincent", "state": "idle",
                            "attributes": {"friendly_name": "Spotify Vincent"}})
    ctx.index.update_state({"entity_id": "switch.ford_rc_start", "state": "off",
                            "attributes": {"friendly_name": "RC Start Ford"}})
    ctx.index.update_state({"entity_id": "light.achtertuin_poort_schijnwerper", "state": "off",
                            "attributes": {"friendly_name": "Achtertuin Poort Schijnwerper"}})
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(event(ctx.index, "media_player.spotify_vincent", "idle", "playing"))
    await watcher.handle_state_changed(event(ctx.index, "light.achtertuin_poort_schijnwerper", "off", "on"))
    await watcher.handle_state_changed(event(ctx.index, "switch.ford_rc_start", "off", "on", context=RESIDENT_CTX))
    assert ctx.db.active_overrides() == {}
    # A resident pressing a light in the app still counts
    await watcher.handle_state_changed(event(ctx.index, "light.keuken", "on", "off", context=RESIDENT_CTX))
    assert list(ctx.db.active_overrides()) == ["light.keuken"]
