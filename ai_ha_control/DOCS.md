# AI HA Control

Een **lokale AI (Ollama)** die je huis helpt beheren via Home Assistant. Alles blijft in je eigen netwerk.

## Wat kan het?

- **Chatten met je huis** via *AI Huis* in de zijbalk, of via **Assist/spraak** met de bijbehorende integratie.
  - "Welke ramen staan open?" · "Hoe warm was het gisteren gemiddeld in de woonkamer?"
  - "Zet de thermostaat op 20,5 graden" · "Doe de lampen in de woonkamer op 30% warm wit"
  - "Staat er een pakketje voor de deur?" (camerabeeld wordt bekeken)
  - "Maak een automatisering die de gang-lamp 's nachts op 10% zet bij beweging"
  - "Onthoud dat ik doordeweeks om 7 uur opsta"
- **Proactief** (zonder dat je iets vraagt):
  - 🔥 Rook, gas, koolmonoxide of lekkage → direct een kritieke melding
  - 📷 Reolink detecteert een persoon/voertuig terwijl niemand thuis is → de AI bekijkt het beeld en stuurt een beschrijving met foto. Bij "niets te zien" geen melding.
  - 🔔 Er wordt aangebeld → melding met beschrijving van wie er staat
  - 🪟 Raam open terwijl de verwarming aan staat
  - 🔓 Slot of garagedeur open terwijl niemand thuis is → melding met knop om het te sluiten
  - ☀️ Ochtendsamenvatting (weer, bijzonderheden van de nacht, lege batterijen, onbereikbare apparaten)
- **Routines leren**: de add-on zoekt elke nacht in het logboek naar handelingen die je vaak rond hetzelfde tijdstip handmatig doet en stelt automatiseringen voor (tabblad *Routines*).
- **Geheugen**: voorkeuren die je vertelt worden onthouden en meegenomen in elk gesprek.

## Veiligheid

- Start standaard in **testmodus** (`dry_run: true`): de AI vertelt wat hij zou doen, maar voert niets uit. Controleer het *Actielog* en zet testmodus daarna uit.
- Sloten, alarm, rolluiken/garagedeuren, kranen en **nieuwe automatiseringen** vragen altijd om bevestiging: in de web-UI, met een knop op je telefoon of door "ja" te zeggen.
- HA herstarten/stoppen, shell-commando's, add-ons beheren en de recorder zijn geblokkeerd.
- Elke actie wordt gelogd in het *Actielog*.

## Installatie

### 1. Ollama (homeserver)

Aanbevolen voor een RTX 2080 Super (8 GB):

| Doel | Model | Grootte |
|------|-------|---------|
| Gesprek + bediening | `qwen3:8b` | ~5,2 GB |
| Camera's | `qwen3-vl:4b` | ~3,3 GB |
| Sneller alternatief | `qwen3:4b` | ~2,6 GB |

```bash
ollama pull qwen3:8b
ollama pull qwen3-vl:4b
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

Flash attention + `q8_0` KV-cache halveren het contextgeheugen, zodat `qwen3:8b` met ~12k context in 8 GB past. Tekst- en beeldmodel passen niet tegelijk; Ollama wisselt automatisch (eerste cameravraag duurt een paar seconden langer). Houd poort 11434 binnen je LAN: Ollama heeft geen wachtwoord.

### 2. Add-on

1. **Instellingen → Add-ons → Add-on store → ⋮ → Repositories** → `https://github.com/paradox101/ai-ha-control`
2. Installeer **AI HA Control**, vul `ollama_url` in (en `notify_service`, zie hieronder) en start de add-on.
3. Open **AI Huis** in de zijbalk. Bovenaan zie je of HA en Ollama verbonden zijn en of de modellen geïnstalleerd zijn.

### 3. Assist / spraak (optioneel)

1. Kopieer `custom_components/ai_ha_control` naar je HA-config (of voeg de repository toe in HACS als *Integration*) en herstart HA.
2. Meestal verschijnt **AI HA Control** vanzelf onder *Instellingen → Apparaten & diensten → Ontdekt*. Zo niet: integratie toevoegen en de URL + API-sleutel uit het tabblad *Koppelen* in AI Huis invullen.
3. *Instellingen → Spraakassistenten* → kies **AI HA Control** als gespreksagent.

De integratie geeft ook de actie `ai_ha_control.ask` (met antwoord), te gebruiken in scripts en automatiseringen.

## Opties

| Optie | Standaard | Uitleg |
|-------|-----------|--------|
| `ollama_url` | – | Adres van Ollama |
| `model` | `qwen3:8b` | Taalmodel (moet tools ondersteunen) |
| `vision_model` | `qwen3-vl:4b` | Model voor camerabeelden |
| `context_size` | `12288` | Context in tokens; hoger = meer VRAM |
| `keep_alive` | `30m` | Hoe lang het model geladen blijft |
| `think` | `false` | Qwen3-denkmodus (nauwkeuriger, veel trager) |
| `language` | `nl` | `nl` of `en` |
| `dry_run` | `true` | Testmodus |
| `only_exposed_entities` | `false` | Alleen entiteiten die bij Assist zijn blootgesteld |
| `confirm_domains` | lock, alarm_control_panel, cover, valve | Acties hierop vragen eerst bevestiging |
| `deny_domains` | shell_command, hassio, recorder, python_script | Nooit toegestaan |
| `deny_services` | restart/stop/reload HA | Nooit toegestaan |
| `max_tool_calls` | `8` | Max. tool-aanroepen per vraag |
| `notify_service` | – | Bijv. `notify.mobile_app_telefoon`. Leeg = meldingen in HA zelf (zonder knoppen/foto) |
| `proactive` | `true` | Proactieve meldingen aan/uit |
| `camera_alerts` | `away` | `off`, `away` (alleen als niemand thuis is; aanbellen altijd) of `always` |
| `open_window_minutes` | `10` | Raam open + verwarming: na zoveel minuten waarschuwen |
| `unlocked_away_minutes` | `15` | Slot/garage open terwijl iedereen weg is: na zoveel minuten |
| `daily_summary_time` | `07:30` | Tijd van de ochtendsamenvatting, leeg = uit |
| `quiet_hours` | `23:00-07:00` | Geen niet-kritieke pushmeldingen (alarmen komen altijd door) |
| `routine_learning` | `true` | Routines herkennen (elke nacht om 03:30) |
| `api_key` | automatisch | Sleutel voor de Assist-integratie |

"Niemand thuis" wordt bepaald aan de hand van je `person.*`-entiteiten. Zonder personen worden afwezigheidsregels overgeslagen.

## Reolink

Gebruik de officiële Reolink-integratie. De add-on koppelt detectiesensoren (persoon, voertuig, dier, bezoeker) automatisch aan de camera van hetzelfde apparaat en gebruikt de *clear*-stream voor de beste beeldkwaliteit.

## Modellen vergelijken

Met `scripts/benchmark.py` in de repository test je modellen op een gesimuleerd huis (er wordt niets in HA uitgevoerd):

```bash
python scripts/benchmark.py --ollama http://<ip-homeserver>:11434 --models qwen3:8b qwen3:4b
```
