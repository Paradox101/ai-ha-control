"""Plain status questions are answered from Home Assistant, without the model (0.6.30)."""

from app.answers import status_answer

from .conftest import FakeLLM


def ask(ctx, question, **kw):
    return status_answer(ctx.index, ctx.config, question, **kw)


def test_lights_on_per_room(ctx):
    assert ask(ctx, "Welke lampen staan aan?") == "Aan in Keuken: Keukenlamp."
    assert ask(ctx, "Staan er lampen aan in de woonkamer?") == "Er staan geen lampen aan in Woonkamer."
    assert ask(ctx, "hoeveel lampen branden er nog") == "Aan in Keuken: Keukenlamp."


def test_open_windows(ctx):
    assert ask(ctx, "Staan er ramen open?") == "Open: Slaapkamer: Raam slaapkamer."
    ctx.index.entities["binary_sensor.raam_slaapkamer"].state = "off"
    assert ask(ctx, "Zijn alle ramen dicht?") == "Alle ramen en deuren met een sensor zijn dicht."


def test_temperatures(ctx):
    assert ask(ctx, "Hoe warm is het buiten?") == "Buiten is het 12 °C."
    assert ask(ctx, "Wat is de temperatuur in de woonkamer?") == "Woonkamer 21,4 °C."
    full = ask(ctx, "Graag alle temperaturen")
    assert full.startswith("Temperatuur per ruimte:\n- Woonkamer 21,4 °C") and full.endswith("Buiten is het 12 °C.")
    assert "\n" not in ask(ctx, "hoe warm is het?", voice=True)


def test_who_is_home(ctx):
    assert ask(ctx, "Wie is er thuis?") == "Thuis: Vincent."
    assert ask(ctx, "Is Vincent thuis?") == "Vincent is thuis."
    ctx.index.entities["person.vincent"].state = "Werk Vincent"
    assert ask(ctx, "Is er iemand thuis?") == "Er is niemand thuis: Vincent is in Werk Vincent."


def test_everything_else_goes_to_the_agent(ctx):
    for question in ("Doe de lampen in de keuken aan", "hoe fel staat de keukenlamp?", "wanneer ging de lamp aan?",
                     "Hoe warm was het vannacht in de slaapkamer?", "hoe gaat het thuis?",
                     "zet de verwarming aan", "waarom staat de lamp aan?", "open het raam", "bedankt",
                     "Welke lampen staan aan in de garage?", "hoe warm is het in de meterkast?"):
        assert ask(ctx, question) is None, question


async def test_the_model_is_not_asked(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry

    ctx.db.add_message("c1", "user", "Welke lampen staan aan?")
    ctx.db.add_message("c1", "assistant", "Geen enkele lamp staat aan.")
    ctx.llm = FakeLLM([{"role": "assistant", "content": "Er staan geen lampen aan."}])
    result = await Agent(ctx, build_registry()).run("Welke lampen staan aan?", "c1")
    assert result.answer == "Aan in Keuken: Keukenlamp." and not ctx.llm.requests


def test_snapshot_is_compact(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry

    prompt = Agent(ctx, build_registry()).system_prompt()
    assert "- Klimaat: Thermostaat (climate.thermostaat): verwarmen, nu 20,5 °C, doel 20 °C" in prompt
    assert "hvac_modes" not in prompt and "min_temp" not in prompt
    assert "- Weer: bewolkt, 12 °C" in prompt


async def test_only_tools_the_question_needs(ctx):
    from app.agent.loop import Agent, chat_tools
    from app.tools.home import build_registry

    registry = build_registry()
    plain = chat_tools(registry, ["Hoe vaak is er vandaag iemand bij de voordeur geweest?"])
    assert {"get_detections", "get_history", "search_entities", "remember"} <= plain
    assert not plain & {"add_watch_rule", "schedule_action", "create_automation", "add_house_rule"}
    assert "schedule_action" in chat_tools(registry, ["zet de lamp om 22:00 uit"])
    assert "add_watch_rule" in chat_tools(registry, ["waarschuw me als de co2 boven 1000 komt"])
    assert "create_automation" in chat_tools(registry, ["maak een automatisering voor de koffie"])
    # The previous question counts: "en om 23:00?" after a light question.
    assert "schedule_action" in chat_tools(registry, ["doe de lamp uit", "en om 23:00?"])

    ctx.llm = FakeLLM([{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "list_watch_rules", "arguments": {}}}]},
        {"role": "assistant", "content": "Er zijn geen bewakingsregels."}])
    result = await Agent(ctx, registry).run("Wat doe je eigenlijk allemaal voor me?", "t1")
    offered = {t["function"]["name"] for t in ctx.llm.requests[0]["tools"]}
    assert "list_watch_rules" not in offered
    # A tool that was not offered still runs when the model asks for it.
    assert result.actions[0]["tool"] == "list_watch_rules" and "error" not in result.actions[0]["result"]


def test_one_device_by_name(ctx):
    assert ask(ctx, "Staat de keukenlamp aan?") == "Keukenlamp: aan."
    assert ask(ctx, "Is het voordeur slot dicht?") == ask(ctx, "is de voordeur op slot?") == "Voordeur slot: op slot."
    assert ask(ctx, "staat de thermostaat aan?") == "Thermostaat: verwarmen, nu 20,5 °C, doel 20 °C."
    assert ask(ctx, "staat het koffiezetapparaat aan?") == "Koffiezetapparaat: uit."
    # Not a device name, or a command: the agent.
    assert ask(ctx, "staat de vaatwasser aan?") is None
    assert ask(ctx, "zet het koffiezetapparaat aan") is None


def test_power_now_and_weather(ctx):
    from .test_house_entities import put

    assert ask(ctx, "Hoeveel stroom verbruiken we nu?") is None  # no meter: the agent says so
    put(ctx, "sensor.p1_meter_power", "-1250", friendly_name="P1 meter Vermogen", device_class="power",
        unit_of_measurement="W")
    put(ctx, "sensor.pv_power", "2100", friendly_name="PV Power", device_class="power", unit_of_measurement="W")
    assert ask(ctx, "Hoeveel stroom verbruiken we nu?") == \
        "Het huis levert nu 1250 W terug aan het net. De zonnepanelen leveren nu 2100 W."
    assert ask(ctx, "Wat leveren de zonnepanelen?") == "De zonnepanelen leveren nu 2100 W."
    assert ask(ctx, "Hoeveel stroom hebben we vandaag verbruikt?") is None
    assert ask(ctx, "Wat is de stroomprijs?") is None  # no price sensor: get_energy
    put(ctx, "sensor.prijs", "0.231", friendly_name="Stroomprijs", unit_of_measurement="€/kWh")
    assert ask(ctx, "Wat is de stroomprijs?") == "De stroomprijs is nu 0,231 €/kWh."
    assert ask(ctx, "Wat voor weer is het?") == "Buiten: bewolkt, 12 °C."
    assert ask(ctx, "Wat wordt het weer morgen?") is None
