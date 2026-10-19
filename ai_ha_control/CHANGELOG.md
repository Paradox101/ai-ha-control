# Changelog

## 0.3.0
- Weersverwachting-tool (`weather.get_forecasts`).
- Meterstanden (kWh, m³): geschiedenis toont het verbruik (toename, ook over een reset heen).
- Camerabeeld zichtbaar in de chat bij een cameravraag.
- Aankondigingen op speakers via TTS (`tts_entity`, `announce_players`) voor deurbel en alarmen.
- Deurbelmeldingen komen ook tijdens stille uren door.
- Watchdog: de Supervisor herstart de add-on als die niet meer reageert.
- Integratietests tegen een echte Home Assistant.

### Opgelost
- Event-handlers blokkeerden de WebSocket: een rookalarm of telefoonknop wachtte 30 s en werd als mislukt gelogd, terwijl HA de actie wel uitvoerde.
- Aliassen van entiteiten worden nu echt geladen (uitgebreide registry-gegevens).
- `only_exposed_entities`: als de lijst niet gelezen kan worden, ziet de AI niets in plaats van alles.
- Mislukte bevestigingen blijven staan zodat je opnieuw kunt proberen.
- Nieuwe/gewijzigde entiteiten, apparaten en ruimtes worden direct opgepikt (zonder herverbinding).
- Detectiesensoren worden op hele woorden herkend (geen "carpet" als "pet").
- Routines rond middernacht worden herkend.
- Ongeldige invoer op de API geeft 400 in plaats van 500.

## 0.2.0
- Assist-integratie (`custom_components/ai_ha_control`): gebruik de lokale AI als gespreksagent, automatisch gevonden via Supervisor discovery.
- Proactief: alarm bij rook/gas/lekkage, Reolink-detectie → camerabeeld laten beoordelen, deurbel, raam open + verwarming, slot/garage open terwijl niemand thuis is (bevestigen via knop op de telefoon).
- Dagelijkse ochtendsamenvatting, stille uren.
- Geheugen: de AI onthoudt voorkeuren (`remember`/`forget`).
- Automatiseringen bekijken en (na bevestiging) aanmaken.
- Routine-herkenning uit het logboek met één-klik automatiseringen.
- Bevestigen met "ja"/"nee" in gesprek of spraak.
- Web-UI: tabbladen Meldingen, Routines, Geheugen, Actielog, Koppelen.
- Benchmark-script om Ollama-modellen te vergelijken.

## 0.1.0
- Eerste versie: Home Assistant-verbinding, Ollama-agent met tools, veiligheidsbeleid, testmodus, actielog, chat-UI.
