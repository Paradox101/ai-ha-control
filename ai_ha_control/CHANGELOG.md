# Changelog

## 0.5.2 — Camera-analyse zonder GPU-geheugenfouten

- Camerabeelden worden verkleind (max. 1280 px) voordat ze naar het beeldmodel gaan, en het beeldmodel draait met een kleine context (4096). Dat scheelt veel VRAM.
- Bij "request exceeds the available context size" wordt het opnieuw geprobeerd met een kleiner beeld.
- Bij "CUDA error: out of memory" ontlaadt de add-on de andere geladen modellen (meestal het chatmodel) en probeert het één keer opnieuw met een kleiner beeld.
- De snapshot wordt in de chat getoond, ook als de analyse mislukt, met een duidelijke foutmelding.

## 0.5.1

- **Aanwezigheid**: alleen de status `home` telt als thuis. Iemand in een andere zone (bijv. "Werk Vincent") wordt gemeld als weg, met de naam van de zone erbij.

## 0.5.0 — Autonome agent

### Autonoom het huis bewaken
- **Hartslag**: elke paar minuten (en direct bij iets opvallends) krijgt de agent een situatierapport en beslist zelf: niets doen, melden, handelen of later opnieuw kijken. Als er niets veranderd is, wordt het LLM niet aangeroepen.
- **Incidenten** met tijdlijn en snapshots: gebeurtenissen die bij elkaar horen worden één verhaal met één melding (persoon bij de poort → in de achtertuin). Verlopen incidenten sluiten automatisch.
- **Opvolging**: de agent plant zelf controles ("kijk over 15 minuten of het raam dicht is").
- **Huismodus**: thuis, weg, slapen, vakantie, gast. Komt uit je modus-scènes (Afwezig/Thuis/Slapen/Gast) als je die hebt, anders uit aanwezigheid en slaaptijden.
- **Wat is normaal**: per sensor en per uur leert de add-on een basislijn (temperatuur, stroom, CO₂, vocht…). Grote afwijkingen, beweging op een uur dat nooit eerder voorkwam en apparaten die wegvallen worden gemeld.
- **Camerapatrouille**: als je weg bent of slaapt, worden camerabeelden goedkoop vergeleken. Alleen bij verschil kijkt het beeldmodel.
- **Huisregels** in gewone taal waar de agent zich aan houdt.
- **Automatiseringen**: de agent ziet wanneer automatiseringen draaien (zodat een scène-wissel niet als verdacht geldt) en kan ze bekijken; nieuwe tool `related_automations` ("wat stuurt scène Afwezig aan?").

### Veilig zelf handelen
- **Autonomie per domein**: kijken / vragen / zelf doen. Standaard: verlichting, verwarming (max. ±2 °C) en ventilatoren zelf; sloten, alarm, garage en de rest alleen na bevestiging.
- **Noodknop** `input_boolean.ai_autonoom` (wordt automatisch aangemaakt). Uit = alleen melden.
- Maximaal aantal eigen acties per uur, alleen omkeerbare acties, en **ongedaan maken** via de melding of de web-UI.

### Minder ruis, betere meldingen
- Het beeldmodel kiest eerst een categorie (niets / voorbijganger / bezoek / verdacht). Voorbijgangers op straat geven geen melding meer.
- 👍/👎 op meldingen; 👎 op een afwijking betekent "dit is normaal" en dempt die.
- Meldingen in Home Assistant vervangen elkaar in plaats van zich op te stapelen.
- Opgelost: persoon-/voertuigdetectie van een deurbel werd als "Er wordt aangebeld" gemeld; alleen de bezoeker-sensor telt nu.
- Geen dubbele meldingen meer tijdens het opstarten van Home Assistant (2 minuten wachttijd; alarmen gaan altijd door).

### Betere antwoorden in de chat
- Antwoordt het model zonder iets te controleren, dan wordt het eenmaal teruggestuurd om het eerst na te kijken (geen verzonnen antwoorden meer).
- Een live momentopname van het huis (aanwezigheid, deuren/ramen, sloten, alarm, klimaat, incidenten) zit in elke vraag.
- Deur-, bewegings- en rookmelders worden als *open/dicht*, *beweging*, *ok* doorgegeven in plaats van `on`/`off`; alarm en sloten in gewone woorden.
- `get_history` met `today` voor vragen als "heeft de stofzuiger vandaag gedraaid?".
- Camera's per ruimte, dubbele streams samengevoegd (scherpe stream), verzonnen camera-ID's worden opgelost.

### Bediening en integratie
- **Alle instellingen in de web-UI**: per onderwerp gegroepeerd, met uitleg, keuzelijsten met je eigen modellen (inclusief tools/beeld-ondersteuning), telefoons, speakers en spraakdienst. Direct toegepast en opgeslagen in de add-on-configuratie.
- Nieuwe tabbladen: Incidenten, Agent, Huisregels. Statuslabels voor huismodus en autonomie.
- Statusentiteiten in Home Assistant: `sensor.ai_huis_status`, `sensor.ai_huismodus`, `binary_sensor.ai_bezorgd`, `sensor.ai_laatste_incident`.
- GPU-wachtrij met prioriteit: veiligheid gaat voor chat, chat voor de agent, de agent voor de patrouille.

## 0.3.1
- Opgelost: "authentication failed: Invalid access" bij het verbinden met Home Assistant. s6-overlay startte de add-on zonder omgevingsvariabelen, waardoor `SUPERVISOR_TOKEN` ontbrak. `run.sh` gebruikt nu `with-contenv`, en het token wordt als vangnet ook uit de s6-omgeving gelezen.

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
