# AI HA Control

Home Assistant add-on + integratie die een **lokale AI (Ollama)** je huis laat beheren: sensoren uitlezen, lampen en klimaat bedienen, Reolink-camera's bekijken, proactief waarschuwen, routines leren en automatiseringen maken. Alles blijft lokaal.

- 📖 Gebruikersdocumentatie: [ai_ha_control/DOCS.md](ai_ha_control/DOCS.md)
- 🗺️ Architectuur en fasering: [PLAN.md](PLAN.md)
- 📝 Wijzigingen: [ai_ha_control/CHANGELOG.md](ai_ha_control/CHANGELOG.md)

## Snel starten

1. Op de homeserver: `ollama pull qwen3:8b && ollama pull qwen3-vl:4b` en laat Ollama op het netwerk luisteren (`OLLAMA_HOST=0.0.0.0`, zie DOCS).
2. Home Assistant → *Add-on store → Repositories* → `https://github.com/paradox101/ai-ha-control` → installeer **AI HA Control**.
3. Vul `ollama_url` en `notify_service` in, start de add-on en open **AI Huis** in de zijbalk.
4. Optioneel voor Assist/spraak: installeer `custom_components/ai_ha_control` (HACS of handmatig).

## Status

| Fase | Inhoud | Status |
|------|--------|--------|
| 0 | Add-on, HA-verbinding, Ollama-client, web-UI | ✅ |
| 1 | Lezen: overzicht, zoeken, toestand, geschiedenis | ✅ |
| 2 | Bedienen + veiligheidsbeleid, testmodus, actielog, bevestigingen | ✅ |
| 3 | Assist-integratie (gespreksagent, discovery, `ai_ha_control.ask`) | ✅ |
| 4 | Camera-analyse (Reolink, vision-model) | ✅ |
| 5 | Proactief: veiligheid, camera-detectie, deurbel, raam+verwarming, onbeveiligd bij afwezigheid, ochtendsamenvatting | ✅ |
| 6 | Geheugen, automatiseringen maken, routine-herkenning | ✅ |
| 7 | Benchmark-script, documentatie, CI | ✅ |
| 8 | Autonome agent: hartslag, incidenten, opvolging, huismodus (scènes), basislijnen en afwijkingen, camerapatrouille, huisregels, feedback, autonomie per domein met noodknop en ongedaan maken, instellingen in de UI, statusentiteiten | ✅ |

## Repository

```
ai_ha_control/            Add-on (Docker, Python/aiohttp)
  app/ha/                 HA WebSocket/REST-client, entity-index
  app/llm/                Ollama-client
  app/agent/              Agent-loop + systeemprompt
  app/tools/              Tools (lezen, bedienen, camera, automatiseringen, geheugen) + beleid/bevestigingen
  app/agent/autonomous.py Autonome agent (hartslag, situatierapport)
  app/autonomy.py         Wat de agent zelf mag + ongedaan maken
  app/settings.py         Instellingen bewerken vanuit de web-UI
  app/watcher/            Waakregels, incidenten, basislijnen, patrouille, planner, routines
  app/web/static/         Ingress web-UI
custom_components/ai_ha_control/   Assist-integratie (gespreksagent)
scripts/benchmark.py      Modellen vergelijken op een gesimuleerd huis
tests/                    Add-on tests (nep-HA + nep-Ollama)
tests_integration/        Integratietests met pytest-homeassistant-custom-component
```

## Ontwikkelen

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # add-on

pip install pytest-homeassistant-custom-component  # aparte venv, Python 3.13
# + de versies van hassil/home-assistant-intents uit HA's conversation-manifest (zie .github/workflows/test.yml)
pytest -c tests_integration/pytest.ini tests_integration

# Lokaal draaien tegen je eigen HA (long-lived access token):
cd ai_ha_control
HA_URL=http://homeassistant.local:8123 HA_TOKEN=... OLLAMA_URL=http://<ip>:11434 \
  DATA_DIR=./data python -m app.main
# → http://localhost:8099
```
