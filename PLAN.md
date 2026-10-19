# AI HA Control – Plan

Home Assistant add-on die een lokaal draaiende LLM (homeserver, RTX 2080 Super / 8 GB VRAM) volledige, maar gecontroleerde toegang geeft tot Home Assistant: entiteiten, sensoren, klimaat, verlichting, camera's, automatiseringen.

---

## 1. Doelen

| # | Doel | Voorbeeld |
|---|------|-----------|
| 1 | **Chatten / spraak** met het huis | "Zet de woonkamer op 20,5 graden en doe de lampen gedimd aan" |
| 2 | **Vragen over de toestand** | "Welke ramen staan open?", "Hoeveel stroom hebben we vandaag verbruikt?" |
| 3 | **Camera-analyse** | "Staat er een pakketje voor de deur?", beschrijving bij beweging |
| 4 | **Proactief beheer** | Waarschuwen bij open raam + verwarming aan, ongebruikelijke beweging als niemand thuis is |
| 5 | **Automatiseringen voorstellen/aanmaken** | "Maak een automatisering die de gang-lamp 's nachts op 10% zet bij beweging" |
| 6 | **Veilig** | Gevoelige acties (sloten, alarm, garagedeur) alleen na bevestiging; volledig auditlog |

Niet-doelen (v1): cloud-LLM's, eigen modeltraining, vervanging van HA-automatiseringen.

---

## 2. Randvoorwaarden

- **Hardware:** RTX 2080 Super = 8 GB VRAM → realistisch:
  - Tekst/tool-calling: 7–8B model in Q4/Q5 (bijv. Qwen2.5/Qwen3 7–8B Instruct, Llama 3.1 8B). Context ~8–16k tokens.
  - Vision: klein VLM (bijv. Qwen2.5-VL 3B/7B Q4, MiniCPM-V, Moondream). Wordt gewisseld met het tekstmodel (Ollama laadt/ontlaadt automatisch) → camera-analyse heeft wat latency.
  - **Gevolg:** we kunnen *nooit* alle entiteiten in de prompt stoppen. Slimme contextselectie is kern van het ontwerp.
- **LLM-runtime:** OpenAI-compatibele API (Ollama, llama.cpp server, LM Studio, vLLM) – configureerbare URL, zodat de LLM op dezelfde of een andere machine kan draaien.
- **HA-installatie:** add-ons vereisen Home Assistant OS of Supervised. (Bij HA Container: zelfde code als losse Docker-container draaien.)

---

## 3. Architectuur

```
┌──────────────────────── Home Assistant OS ─────────────────────────┐
│                                                                    │
│  ┌──────────── Add-on: ai-ha-control (Docker, Python) ──────────┐  │
│  │                                                              │  │
│  │  Ingress Web UI (chat, log, instellingen)                    │  │
│  │        │                                                     │  │
│  │  ┌─────▼──────┐   ┌──────────────┐   ┌───────────────────┐   │  │
│  │  │ Agent-loop │◄─►│ Tool-laag    │◄─►│ HA-client          │◄──┼──┼── WebSocket + REST
│  │  │ (LLM +     │   │ (functies +  │   │ (SUPERVISOR_TOKEN) │   │  │   (http://supervisor/core)
│  │  │  tools)    │   │  policy)     │   └───────────────────┘   │  │
│  │  └─────┬──────┘   └──────┬───────┘                           │  │
│  │        │                 │        ┌───────────────────┐      │  │
│  │        │                 └───────►│ Entity-index      │      │  │
│  │        │                          │ (areas, domains,  │      │  │
│  │        │                          │  aliassen, zoek)  │      │  │
│  │  ┌─────▼──────┐   ┌────────────┐  └───────────────────┘      │  │
│  │  │ Event-     │   │ Geheugen + │                             │  │
│  │  │ watcher /  │   │ auditlog   │                             │  │
│  │  │ scheduler  │   │ (SQLite)   │                             │  │
│  │  └────────────┘   └────────────┘                             │  │
│  └──────────────────────────────┬───────────────────────────────┘  │
│                                 │                                  │
│  Custom integration (conversation agent) ── Assist / spraak ───────┤
└─────────────────────────────────┼──────────────────────────────────┘
                                  │ OpenAI-compatible API
                     ┌────────────▼────────────┐
                     │ Homeserver (RTX 2080S)  │
                     │ Ollama / llama.cpp      │
                     │  - tekstmodel (tools)   │
                     │  - vision-model         │
                     └─────────────────────────┘
```

### 3.1 Componenten

1. **HA-client** (`app/ha/`)
   - WebSocket: `get_states`, area/device/entity registry, `subscribe_events`, `call_service`, `camera_thumbnail`, history/statistics, automation config.
   - REST als fallback (`/api/camera_proxy/...`, `/api/history/...`).
   - Auth via `SUPERVISOR_TOKEN` (`homeassistant_api: true` in add-on config).

2. **Entity-index** (`app/ha/index.py`)
   - Houdt live een compacte kaart bij: `area → device → entity` met friendly name, aliassen, domein, device_class, unit.
   - Zoekfunctie (fuzzy + area/domein-filter), zodat het model eerst *zoekt* en daarna pas details opvraagt.
   - Compacte state-weergave (`sensor.woonkamer_temp=21.3°C`) om tokens te sparen.
   - Respecteert HA's "exposed entities" (Assist-instellingen) + eigen allow/deny-lijst.

3. **Tool-laag** (`app/tools/`) – functies die het LLM mag aanroepen:

   | Tool | Doel |
   |------|------|
   | `get_home_overview` | Korte samenvatting: wie thuis, areas, actieve alarmen, afwijkingen |
   | `search_entities(query, area?, domain?)` | Entiteiten vinden |
   | `get_state(entity_ids)` | Actuele staat + relevante attributen |
   | `get_history(entity_id, period)` | Geaggregeerde historie (min/max/gem, niet ruwe data) |
   | `call_service(domain, service, target, data)` | Generieke actie (onder policy) |
   | `set_climate(entity, temp/mode/preset)` | Klimaat-shortcut |
   | `control_light(entity, on/off, brightness, color)` | Licht-shortcut |
   | `analyze_camera(entity, question)` | Snapshot → vision-model → tekstueel antwoord |
   | `list_automations` / `propose_automation(yaml)` | Automatiseringen bekijken / voorstellen (aanmaken na bevestiging) |
   | `notify(message, target)` | Melding naar telefoon |
   | `remember(fact)` / `recall(query)` | Langetermijngeheugen (voorkeuren, routines) |

   Specifieke shortcuts naast `call_service` → kleine modellen maken minder fouten met eenvoudige schema's.

4. **Policy / veiligheid** (`app/policy.py`)
   - Per domein/entiteit niveau: `allow` / `confirm` / `deny`.
   - Standaard `confirm`: `lock`, `alarm_control_panel`, `cover.garage*`, `homeassistant.restart`, automatisering wijzigen/verwijderen.
   - Standaard `deny`: `shell_command`, `hassio.*`, `recorder.purge`, etc.
   - Validatie van tool-argumenten (bestaat entity? geldige range voor temperatuur?).
   - Rate limit + max. tool-calls per beurt (voorkomt loops).
   - Dry-run modus voor testen.
   - Auditlog van élke actie (wie/wat/wanneer/waarom).

5. **Agent-loop** (`app/agent/`)
   - OpenAI-compatibele chat + tool-calling; fallback naar JSON-mode/prompt-parsing voor modellen zonder native tool support.
   - Systeemprompt (NL) met huisprofiel + huidige tijd + aanwezigheid; rest via tools.
   - Contextbeheer: gesprekken samenvatten, oude tool-output inkorten.
   - Max. N iteraties per verzoek.

6. **Event-watcher / proactief** (`app/watcher/`)
   - Luistert op `state_changed`, filtert met **regels zonder LLM** (goedkoop): bijv. beweging + niemand thuis, raam open + verwarming aan, CO₂ > drempel, sensor offline.
   - Alleen bij een trigger wordt het LLM ingezet om te beoordelen/samenvatten en evt. actie voor te stellen.
   - Geplande taken: ochtend-/avondsamenvatting, energierapport.
   - Optioneel: Frigate-events (object detectie) als trigger voor camera-analyse.

7. **Geheugen + opslag** (`/data` in add-on)
   - SQLite: gesprekken, auditlog, feiten/voorkeuren, event-samenvattingen.
   - Optioneel later: embeddings voor semantisch zoeken (klein embed-model).

8. **Interfaces**
   - **Ingress web-UI**: chat, actielog, openstaande bevestigingen, instellingen. (FastAPI + lichte frontend.)
   - **Custom integration** `custom_components/ai_ha_control`: registreert een *conversation agent* → werkt met Assist, spraak-satellieten en HA-app.
   - **Bevestigingen** via actionable notifications (Ja/Nee-knoppen in HA Companion app).
   - **MCP-server** (optioneel): dezelfde tools beschikbaar maken via MCP voor andere clients.

---

## 4. Repo-structuur

```
ai-ha-control/
├── repository.yaml                 # HA add-on repository
├── ai_ha_control/                  # de add-on
│   ├── config.yaml                 # add-on manifest (ingress, homeassistant_api, options)
│   ├── Dockerfile
│   ├── run.sh
│   ├── translations/{en,nl}.yaml
│   └── app/
│       ├── main.py                 # FastAPI + startup
│       ├── config.py
│       ├── ha/ (client.py, index.py, camera.py)
│       ├── llm/ (client.py, prompts.py)
│       ├── agent/ (loop.py, context.py)
│       ├── tools/ (registry.py, states.py, services.py, climate.py, camera.py, automations.py, memory.py)
│       ├── policy.py
│       ├── watcher/ (rules.py, scheduler.py)
│       ├── storage/ (db.py)
│       └── web/ (routes + static UI)
├── custom_components/ai_ha_control/  # conversation agent (installeerbaar via HACS)
└── tests/                          # pytest, met gemockte HA + LLM
```

Add-on opties (voorbeeld):

```yaml
llm_url: "http://192.168.1.10:11434/v1"
llm_model: "qwen2.5:7b-instruct"
vision_model: "qwen2.5vl:3b"
language: "nl"
dry_run: true
confirm_domains: [lock, alarm_control_panel, cover]
deny_domains: [shell_command, hassio]
proactive: true
```

---

## 5. Fasering

| Fase | Inhoud | Resultaat |
|------|--------|-----------|
| **0. Fundament** | Add-on skeleton, config, Dockerfile, HA WebSocket-client, LLM-client, verbindingstest in UI | Add-on installeert en "ziet" HA + LLM |
| **1. Lezen** | Entity-index, `search_entities`, `get_state`, `get_history`, `get_home_overview`; chat-UI | "Welke ramen staan open?" werkt |
| **2. Bedienen** | `call_service` + shortcuts, policy-engine, dry-run, auditlog, bevestigingsflow | Veilig lampen/klimaat bedienen |
| **3. Assist** | Custom conversation-integration | Spraak/Assist gebruikt de lokale AI |
| **4. Camera's** | Snapshot-pipeline, vision-model, `analyze_camera`, optioneel Frigate | "Staat er iemand voor de deur?" |
| **5. Proactief** | Event-watcher, regels, LLM-beoordeling, notificaties, dagelijkse samenvatting | Huis waarschuwt zelf |
| **6. Geheugen & automatiseringen** | Voorkeuren onthouden, routines herkennen, automatiseringen voorstellen/aanmaken | AI leert het huis kennen |
| **7. Polish** | Model-benchmark (tool-call nauwkeurigheid NL), prompt-tuning, docs, HACS/add-on store | Release v1.0 |

Testen per fase: pytest met mock HA-websocket + mock LLM; een vaste set NL-testopdrachten om modellen te vergelijken op juiste tool-keuze.

---

## 6. Risico's & mitigatie

| Risico | Mitigatie |
|--------|-----------|
| Klein model kiest verkeerde entiteit/actie | Zoek-eerst-patroon, eenvoudige tool-schema's, validatie, bevestiging bij twijfel |
| Te weinig context (8 GB VRAM) | Compacte state-weergave, entity-index, samenvatten, geen volledige state-dumps |
| Vision + tekstmodel passen niet tegelijk in VRAM | Modellen wisselen (Ollama `keep_alive`), camera-analyse async, kleinere VLM |
| Ongewenste acties (sloten, alarm) | Policy `confirm`/`deny`, auditlog, dry-run, rate limits |
| Prompt-injectie via entity-namen/camera-tekst | Tool-output als data markeren, gevoelige acties altijd bevestigen |
| Latency | Streaming, regelgebaseerde voorfilter voor proactieve taken, korte prompts |
| Privacy | Alles lokaal; geen externe calls; camera-snapshots niet opslaan (tenzij ingesteld) |

---

## 7. Keuzes (beantwoorde vragen)

| Vraag | Antwoord | Gevolg |
|-------|----------|--------|
| LLM-runtime | Ollama, model naar advies | `qwen3:8b` (tekst/tools) + `qwen3-vl:4b` (camera). Native Ollama API (`/api/chat`) voor `num_ctx`, `think`, `keep_alive` en beelden |
| Waar draait HA | HA OS op aparte machine, zelfde netwerk | Ollama luistert op `0.0.0.0:11434`; add-on verbindt via `ollama_url` |
| Camera's | Reolink (geen Frigate) | Snapshots via HA `camera_proxy`; Reolink persoon/voertuig-sensoren als trigger in fase 5 |
| Assist/spraak | Nog niet | Fase 3 later; eerst Ingress-chat |
