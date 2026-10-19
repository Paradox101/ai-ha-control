# AI HA Control

Home Assistant add-on die een **lokale AI (Ollama)** je huis laat beheren: sensoren uitlezen, lampen en klimaat bedienen, camera's (Reolink) bekijken. Alles blijft lokaal.

Zie [PLAN.md](PLAN.md) voor de architectuur en de fasering.

## Status

| Fase | Inhoud | Status |
|------|--------|--------|
| 0 | Add-on, HA-verbinding, Ollama-client, web-UI | ✅ |
| 1 | Lezen: overzicht, zoeken, toestand, geschiedenis | ✅ |
| 2 | Bedienen: lampen, klimaat, aan/uit, services + veiligheidsbeleid, testmodus, actielog, bevestigingen | ✅ |
| 4 | Camera-analyse (snapshot → vision-model) | ✅ basis |
| 3, 5, 6 | Assist, proactief, geheugen/automatiseringen | gepland |

## 1. Ollama instellen (homeserver met RTX 2080 Super)

Aanbevolen modellen voor 8 GB VRAM:

| Doel | Model | Grootte | Waarom |
|------|-------|---------|--------|
| Gesprek + bediening | `qwen3:8b` | ~5,2 GB | Beste tool-calling in deze klasse, goed Nederlands |
| Camera's | `qwen3-vl:4b` | ~3,3 GB | Klein, goed in beeldbeschrijving |
| Alternatief (sneller/minder VRAM) | `qwen3:4b` | ~2,6 GB | Als 8b te traag is of je meer context wilt |

```bash
ollama pull qwen3:8b
ollama pull qwen3-vl:4b
```

Home Assistant draait op een andere machine, dus Ollama moet op het netwerk luisteren. Op Linux (systemd):

```bash
sudo systemctl edit ollama
```

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

```bash
sudo systemctl restart ollama
curl http://<ip-homeserver>:11434/api/tags   # test vanaf een andere machine
```

- `FLASH_ATTENTION` + `KV_CACHE_TYPE=q8_0` halveren het geheugen voor de context, zodat `qwen3:8b` met ~12k context in 8 GB past.
- `MAX_LOADED_MODELS=1`: tekst- en beeldmodel passen niet samen in 8 GB. Ollama wisselt automatisch, dus een camera-vraag duurt de eerste keer een paar seconden langer.
- Zorg dat poort 11434 alleen binnen je LAN bereikbaar is (Ollama heeft geen authenticatie).

## 2. Add-on installeren

1. Home Assistant → **Instellingen → Add-ons → Add-on store → ⋮ → Repositories**
2. Voeg toe: `https://github.com/paradox101/ai-ha-control`
3. Installeer **AI HA Control**
4. Stel bij **Configuratie** `ollama_url` in op `http://<ip-homeserver>:11434`
5. Start de add-on en open **AI Huis** in de zijbalk

De add-on start in **testmodus** (`dry_run: true`): de AI zegt wat hij zou doen, maar voert niets echt uit. Controleer het actielog en zet daarna `dry_run` uit.

## 3. Opties

| Optie | Standaard | Uitleg |
|-------|-----------|--------|
| `ollama_url` | – | Adres van Ollama |
| `model` | `qwen3:8b` | Taalmodel (moet tools ondersteunen) |
| `vision_model` | `qwen3-vl:4b` | Model voor camerabeelden |
| `context_size` | `12288` | Context in tokens; hoger = meer VRAM |
| `keep_alive` | `30m` | Hoe lang het model geladen blijft |
| `think` | `false` | Qwen3-denkmodus (nauwkeuriger, veel trager) |
| `dry_run` | `true` | Testmodus |
| `only_exposed_entities` | `false` | Alleen entiteiten die bij Assist zijn blootgesteld |
| `confirm_domains` | lock, alarm, cover, valve | Acties hierop vragen eerst bevestiging in de UI |
| `deny_domains` | shell_command, hassio, recorder, python_script | Nooit toegestaan |
| `deny_services` | restart/stop HA | Nooit toegestaan |
| `max_tool_calls` | `8` | Max. tool-aanroepen per vraag |

Garagedeuren/poorten en sloten vragen altijd bevestiging, ook als je `cover` uit `confirm_domains` haalt.

## 4. Reolink-camera's

Gebruik de officiële **Reolink**-integratie in Home Assistant. De `camera.*`-entiteiten (bijv. `camera.voordeur_fluent`) zijn direct bruikbaar: vraag bijvoorbeeld *"Staat er een pakketje voor de deur?"*. De Reolink detectiesensoren (persoon/voertuig/dier) worden in fase 5 gebruikt om de AI proactief te laten kijken.

## 5. Voorbeelden

- "Welke ramen staan open?"
- "Hoe warm was het gisteren gemiddeld in de woonkamer?"
- "Zet de thermostaat op 20,5 graden"
- "Doe de lampen in de woonkamer op 30% warm wit"
- "Wat zie je op de camera bij de oprit?"

## Ontwikkelen

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest

# Lokaal draaien tegen je eigen HA (long-lived access token):
cd ai_ha_control
HA_URL=http://homeassistant.local:8123 HA_TOKEN=... OLLAMA_URL=http://<ip>:11434 \
  DATA_DIR=./data python -m app.main
# → http://localhost:8099
```
