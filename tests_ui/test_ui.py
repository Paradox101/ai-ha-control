"""Headless browser check of the web UI against the real add-on with a fake Home Assistant and Ollama."""

import asyncio
import os
import time

import pytest
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from app import settings as settings_module
from app.config import Config
from app.main import create_app
from tests.conftest import STATES
from tests.test_e2e import fake_home_assistant, fake_ollama

RUNNING = {}  # the add-on state of the running test server
# Markdown as the model writes it, plus HTML that must stay text.
ANSWER = "**Alles** is rustig thuis.\n\n**Personen**\n- Vincent: *thuis*\n  - sinds `10:00`\n- <img src=x onerror=alert(1)>"
TABS = ["incidents", "agent", "events", "rules", "routines", "memory", "log", "settings", "pair", "chat"]


@pytest.fixture
async def addon(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "options.json"))
    ha_server = TestServer(fake_home_assistant([]))
    ollama_server = TestServer(fake_ollama([], [{"role": "assistant", "content": ANSWER}]))
    await ha_server.start_server()
    await ollama_server.start_server()
    config = Config(ha_url=str(ha_server.make_url("")).rstrip("/"), ha_token="secret",
                    ollama_url=str(ollama_server.make_url("")).rstrip("/"), data_dir=str(tmp_path))
    server = TestServer(create_app(config))
    await server.start_server()
    state = server.app["state"]
    await state.ha.wait_connected(5)
    for _ in range(50):
        if len(state.index.entities) >= len(STATES):
            break
        await asyncio.sleep(0.05)
    RUNNING["state"] = state
    yield str(server.make_url("/"))
    await server.close()
    await ha_server.close()
    await ollama_server.close()


async def test_ui_tabs_chat_and_settings(addon):
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.goto(addon)
        await page.wait_for_selector("#ha:has-text('HA ✓')")
        await page.click("#export")
        await page.wait_for_selector("#export-panel:not(.hidden)")
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        async with page.expect_download() as download:
            await page.click("#export-xlsx")
        assert (await download.value).suggested_filename.endswith(".xlsx")
        # Where downloading fails (the Home Assistant app), the text can be copied instead.
        await page.click("#export-copy")
        await page.wait_for_selector("#export-text:not(.hidden)")
        text = await page.input_value("#export-text")
        assert "light.keuken" in text and "\t" in text
        await page.wait_for_selector("#toast:has-text('regels')")
        await page.click("#export-close")
        await page.wait_for_selector("#export-panel.hidden", state="attached")

        for tab in TABS:
            await page.click(f"#nav button[data-tab='{tab}']")
            await page.wait_for_selector(f"#tab-{tab}:not(.hidden)")
        await page.click("#nav button[data-tab='agent']")
        await page.wait_for_selector("#agent-perf .empty, #agent-perf .card")

        await page.click("#nav button[data-tab='chat']")
        await page.fill("#input", "Hoe gaat het thuis?")
        await page.click("#send")
        await page.wait_for_selector(".msg:not(.user):not(.typing):has-text('Alles is rustig thuis.')")
        answer = page.locator(".msg:not(.user):not(.typing)").last
        assert "*" not in await answer.inner_text()
        assert await answer.locator("strong").first.inner_text() == "Alles"
        assert await answer.locator("ul > li > ul > li code").inner_text() == "10:00"
        assert await answer.locator("img").count() == 0
        assert "<img src=x" in await answer.inner_text()

        await page.click("#nav button[data-tab='agent']")
        await page.wait_for_selector("#agent-perf .card:has-text('qwen3:8b')")

        await page.click("#nav button[data-tab='settings']")
        await page.wait_for_selector("text=Agenda gebruiken")
        await page.wait_for_selector("text=Energieprijzen gebruiken")
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        await browser.close()
    assert not errors, errors


async def test_settings_save_with_unknown_field_type(addon, monkeypatch):
    """A field type the page has no builder for (stale cached page) must not break rendering or saving."""
    sections = [{**s, "fields": list(s["fields"])} for s in settings_module.SECTIONS]
    sections[0]["fields"].append({"key": "future_field", "label": "Toekomstig veld", "type": "future", "help": ""})
    monkeypatch.setattr(settings_module, "SECTIONS", sections)
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = await browser.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        response = await page.goto(addon)
        assert response.headers.get("cache-control") == "no-cache"
        await page.wait_for_selector("#ha:has-text('HA ✓')")
        await page.click("#nav button[data-tab='settings']")
        await page.wait_for_selector("text=Herlaad de pagina")
        await page.wait_for_selector("text=Agenda gebruiken")
        await page.evaluate("document.querySelector(\"input[aria-label='Denkmodus']\").click()")
        await page.click("#settings-save")
        await page.wait_for_selector("text=Opgeslagen en direct toegepast")
        await browser.close()
    assert not errors, errors


async def test_ui_comfort_backup_heating(addon):
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.goto(addon)
        await page.wait_for_selector("#ha:has-text('HA ✓')")
        await page.click("#nav button[data-tab='comfort']")
        await page.wait_for_selector("#profiles .empty")
        await page.click("#profile-add")
        card = page.locator("#profiles .card").first
        await card.locator("details.backup summary").click()
        await card.locator("details.backup select").select_option("climate.thermostaat")
        await card.locator("input[name='backup_below']").fill("-3")
        await card.locator("input[name='backup_after_minutes']").fill("45")
        await card.locator("select[name='temperature_sensor']").select_option("sensor.woonkamer_temperatuur")
        await card.locator("button:has-text('Opslaan')").click()
        await page.wait_for_selector("text=Profiel opgeslagen")
        data = await page.evaluate("fetch('api/profiles').then(r => r.json())")
        (profile,) = data["profiles"]
        assert profile["backup_entity"] == "climate.thermostaat"
        assert profile["backup"] == {"below": -3, "after_minutes": 45}
        assert profile["temperature_sensor"] == "sensor.woonkamer_temperatuur"
        await page.wait_for_selector("#profiles details.backup[open]")
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        if os.environ.get("UI_SCREENSHOT"):
            await page.screenshot(path=os.environ["UI_SCREENSHOT"], full_page=True)
        await browser.close()
    assert not errors, errors


async def test_ui_alarm_seen_and_watch_rules(addon):
    errors = []
    state = RUNNING["state"]
    await state.incidents.report("security", "Mogelijke inbraak", "Achterdeur ging open", severity="critical",
                                 critical=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.goto(addon)
        await page.wait_for_selector("#ha:has-text('HA ✓')")

        await page.click("#nav button[data-tab='incidents']")
        await page.click("#incidents button:has-text('Gezien')")
        await page.wait_for_selector("#incidents .card:has-text('Mogelijke inbraak')")
        await page.wait_for_selector("#incidents button:has-text('Gezien')", state="detached")
        assert state.db.incidents(status="active")[0]["unseen"] == 0

        await page.click("#nav button[data-tab='rules']")
        await page.wait_for_selector("#watch-entity option[value='sensor.woonkamer_temperatuur']", state="attached")
        await page.select_option("#watch-entity", "sensor.woonkamer_temperatuur")
        await page.select_option("#watch-op", ">")
        await page.fill("#watch-value", "25")
        await page.click("#watch-add")
        await page.wait_for_selector("#watch-rules .card:has-text('boven 25')")
        assert state.db.watch_rules()[0]["entity_id"] == "sensor.woonkamer_temperatuur"

        state.db.add_scheduled(time.time() + 3600, "reminder", "Vuilnisbak buiten zetten", {})
        await page.click("#nav button[data-tab='events']")
        await page.click("#scheduled .card:has-text('Vuilnisbak') button:has-text('Annuleren')")
        await page.wait_for_selector("#scheduled .card", state="detached")
        assert state.db.scheduled() == []
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        await browser.close()
    assert not errors, errors
