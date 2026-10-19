"""Headless browser check of the web UI against the real add-on with a fake Home Assistant and Ollama."""

import asyncio
import os

import pytest
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from app import settings as settings_module
from app.config import Config
from app.main import create_app
from tests.conftest import STATES
from tests.test_e2e import fake_home_assistant, fake_ollama

TABS = ["incidents", "agent", "events", "rules", "routines", "memory", "log", "settings", "pair", "chat"]


@pytest.fixture
async def addon(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "options.json"))
    ha_server = TestServer(fake_home_assistant([]))
    ollama_server = TestServer(fake_ollama([], [{"role": "assistant", "content": "Alles is rustig thuis."}]))
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
        async with page.expect_download() as download:
            await page.click("#export")
        assert (await download.value).suggested_filename.endswith(".xlsx")

        for tab in TABS:
            await page.click(f"#nav button[data-tab='{tab}']")
            await page.wait_for_selector(f"#tab-{tab}:not(.hidden)")
        await page.click("#nav button[data-tab='agent']")
        await page.wait_for_selector("#agent-perf .empty, #agent-perf .card")

        await page.click("#nav button[data-tab='chat']")
        await page.fill("#input", "Hoe gaat het thuis?")
        await page.click("#send")
        await page.wait_for_selector(".msg:not(.user):not(.typing):has-text('Alles is rustig thuis.')")

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
        await card.locator("button:has-text('Opslaan')").click()
        await page.wait_for_selector("text=Profiel opgeslagen")
        data = await page.evaluate("fetch('api/profiles').then(r => r.json())")
        (profile,) = data["profiles"]
        assert profile["backup_entity"] == "climate.thermostaat"
        assert profile["backup"] == {"below": -3, "after_minutes": 45}
        await page.wait_for_selector("#profiles details.backup[open]")
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        if os.environ.get("UI_SCREENSHOT"):
            await page.screenshot(path=os.environ["UI_SCREENSHOT"], full_page=True)
        await browser.close()
    assert not errors, errors
