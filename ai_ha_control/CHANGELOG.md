# Changelog

## 0.6.8

- **Alarm niet gezien**: kritieke meldingen hebben een knop *Gezien*. Zonder reactie na `escalate_minutes` (standaard 5) gaat de melding opnieuw naar iedereen en wordt hij omgeroepen, maximaal 3 keer. Ook *Gezien* in het tabblad Incidenten.
- **Bewakingsregels**: "waarschuw als de vriezer boven -15 graden komt" wordt een vaste regel (entiteit, boven/onder/is, waarde, minimale duur, optioneel alleen in bepaalde huismodi). De add-on controleert hem bij elke wijziging zelf, zonder de AI, en meldt het als het weer normaal is. Aanmaken via de chat (met bevestiging) of in het tabblad Huisregels.
- Voorbeeldknoppen in het tabblad Huisregels lopen op een telefoon niet meer buiten beeld.

## 0.6.7

- Opslaan van instellingen gaf soms "builders[spec.type] is not a function": na een update draaide de browser nog een oude, gecachte pagina. De pagina wordt nu altijd opnieuw gecontroleerd en een onbekend veld toont een melding in plaats van het formulier te breken.

## 0.6.6

- Comfort: per ruimte een cv-thermostaat als backup naast de airco. De cv springt alleen bij onder een instelbare buitentemperatuur, als de airco de setpoint niet binnen een instelbare tijd haalt, of bij storing of lang ontdooien; nooit terwijl de airco koelt. Het Comfort-tabblad toont welke bron nu verwarmt.

## 0.6.5 — "Hoe vaak / wanneer / hoe lang" klopt

- Nieuwe tool **get_detections**: telt exact hoe vaak en wanneer er een persoon, voertuig, dier, pakketje, aanbelling of beweging was, per camera en over een periode (vandaag, gisteren, vannacht, een weekdag, deze week, een datum). Dubbele sensoren van de Duidelijk/Vloeiend-streams, flikkerende sensoren en een korte "onbeschikbaar" tellen als één keer.
- **get_history** telt nu zelf: aantal keer aan/open/thuis, de tijdstippen en de totale duur. De toestand aan het begin van de periode telde voorheen als wijziging; daardoor werd "camera idle om 00:00" gelezen als "één keer een persoon gezien".
- De camera-status (idle/recording) wordt niet meer gebruikt voor detectievragen; het model neemt aantallen en tijden letterlijk over.
- **⬇ Excel**-knop in de web-UI: alle entiteiten die de add-on ziet, plus de camera-indeling van detectiesensoren.
- 11 evaluatievragen (tests en `scripts/benchmark.py`) met een gesimuleerde dag geschiedenis.

## 0.6.3

- Inbraakmelding en de melding "AI-server niet bereikbaar" werkten niet vlak na het opstarten van het systeem (de klok begon te dicht bij nul en telde als "net thuisgekomen").

## 0.6.2 — Hardening, snellere Assist en echte entiteiten

### Beveiliging en betrouwbaarheid
- **AppArmor-profiel**: de add-on zelf mag alleen nog `/data` schrijven; code, HA-config, back-ups en media zijn afgeschermd.
- **Health-check** meldt een probleem als Home Assistant >5 min niet verbonden is of de scheduler vastzit; de Supervisor-watchdog herstart de add-on dan.
- **Database**: WAL-modus, indexen en nachtelijke opschoning (gesprekken 30 dagen, meldingen 180 dagen, audit en acties 1 jaar).
- **Back-ups**: vóór een HA-back-up wordt een consistente kopie van de database gemaakt; snapshots en het live-bestand gaan niet meer mee. Na terugzetten wordt die kopie automatisch gebruikt.
- Beveiligingsheaders (CSP, nosniff), limiet op mislukte API-sleutelpogingen, maximale berichtlengte en een API-sleutelbestand dat alleen de add-on kan lezen.

### Assist
- **Ruimte van de spraaksatelliet** gaat mee: "doe het licht aan" in de keuken betekent de keuken.
- **Streaming**: het antwoord komt woord voor woord binnen, zodat de satelliet eerder begint te praten.
- Tip in de documentatie: laat HA simpele commando's zelf afhandelen ("Prefer handling commands locally").

### Integratie
- Echte entiteiten met unique_id: huisstatus, huismodus, laatste incident, open incidenten, wacht op bevestiging, AI-reactietijd, bezorgd, add-on-probleem en een schakelaar *Zelf handelen*. De oude losse statussensoren verdwijnen zodra de integratie actief is.
- Event `ai_ha_control_incident` bij een nieuw of verergerd incident, voor eigen automatiseringen.

### Slimmer
- Camera-analyse vraagt het model om JSON (categorie + beschrijving) in plaats van vrije tekst.
- **AI-prestaties** in het tabblad Agent: duur, tokens, wachttijd en modelwissels per model.
- **Agenda**: afspraken in de ochtendsamenvatting en via de chat; een lopende afspraak met "vakantie" of "gasten" zet de huismodus.
- **Energie**: stroomprijs, goedkoopste tijdvak en zonneverwachting in de samenvatting en via de chat (alleen informatie).
- Ochtendsamenvatting en routinemelding in het Engels als de taal op `en` staat.

### Onderhoud
- aiohttp 3.13.5, basisimage Alpine 3.22, Dependabot, en een workflow die kant-en-klare images naar ghcr publiceert.
- CI: browsertest van de web-UI, test tegen een echte Home Assistant, en de add-on draaiend onder zijn AppArmor-profiel.

## 0.6.0 — Nauwkeuriger, veiliger en comfort

### Beveiliging
- **Deuren en alarm nooit zelf**, ook niet als een deur- of poortopener als schakelaar, knop, scène of script in HA staat. Gevoelig is alles op hetzelfde apparaat als een slot, alarmpaneel of garage/poort, plus namen met deur, poort, garage, slot, alarm, gate, door… Sloten en alarm kunnen in de instellingen niet meer op *Zelf doen*.
- **Scènes en scripts** start de agent alleen zelf als je ze vrijgeeft (`autonomous_scenes`).
- **Niet via spraak bevestigen**: "ja" tegen een spraakassistent bevestigt geen sloten, alarm of poorten meer; dat kan alleen op je telefoon of in de web-UI.
- **Directe inbraakmelding**: gaat een deur of raam open terwijl iedereen weg is of het alarm aan staat, dan komt er meteen een kritieke melding met snapshot van de dichtstbijzijnde camera, zonder op de AI te wachten. 's Nachts (slapen) een waarschuwing bij deuren; 👎 = normaal. Net thuisgekomen telt niet. Ook *alarm afgegaan* wordt direct kritiek gemeld.
- **Noodgevallen**: bij lekkage de hoofdkraan dicht (`leak_valve`), bij rook/gas/CO alle lampen aan en ventilatie uit. Met ongedaan maken.
- **API-sleutel** geeft alleen nog toegang tot het gesprek (Assist); instellingen, noodknop enz. alleen via het HA-paneel.
- Bevestigingen blijven bewaard na een herstart van de add-on.
- Camerabeschrijvingen gaan gemarkeerd («…») en ingekort naar de agent, zodat tekst in beeld geen opdracht kan worden.

### Nauwkeuriger
- **Wie deed het?** Elke wijziging in het situatierapport staat met wie het deed: bewoner, automatisering (met naam), de agent, de AI op verzoek of het apparaat zelf. Op basis van de context van Home Assistant, niet meer geraden.
- **Niet vechten met de bewoner**: past iemand zelf een lamp, thermostaat of rolluik aan, dan blijft de agent er `override_minutes` (standaard 2 uur) vanaf. Draait iemand een actie van de agent terug, dan telt dat als 👎.
- **Controle na een actie**: na ~45 s kijkt de agent of het apparaat echt reageerde, probeert het één keer opnieuw en meldt anders "reageert niet".
- **Hartslag apart**: een trage AI houdt de regels, de patrouille en de basislijn niet meer op.
- **Rijker situatierapport**: per ruimte temperatuur (met doel en trend), vocht en CO₂; zon en weer.
- **Basislijn per soort dag**: werkdagen en weekend apart, en oude metingen tellen langzaam minder (seizoenen).
- **Schaduwmodus** per domein: de agent legt vast wat hij zou doen zonder het te doen. Beoordeel met 👍/👎 in het tabblad *Agent* en zet het domein daarna op *Zelf doen*.
- **Replay**: situatierapporten worden bewaard; met `scripts/replay.py` vergelijk je modellen op je eigen huis. 👍/👎 op een controle in het tabblad *Agent* telt daarbij als beoordeling.
- **Dodemansknop**: `sensor.ai_huis_status` heeft nu `last_tick`, `last_agent_run` en `ollama`. Is de AI-server langer dan `llm_down_minutes` weg, dan krijg je één melding (en een als hij terug is).

### Comfort
- **Comfortprofielen** (nieuw tabblad *Comfort*, of in de chat: "'s nachts wil ik 17 graden in de slaapkamer"): per ruimte de temperatuur per huismodus, lampen bij schemer, ventilatie bij hoog CO₂/vocht en zonwering bij felle zon. Uitgevoerd door vaste regels, niet door de AI, en alleen bij een veranderde wens.
- **Voorverwarmen** voor de verwachte thuiskomst: als iemand onderweg is (proximity-integratie) of op basis van de geleerde thuiskomsttijd per weekdag; bij kou eerder.
- **Leren van correcties**: zet je de thermostaat vaak met de hand anders dan het profiel, dan stelt de add-on voor het profiel aan te passen.
- **Meldingen per persoon** (`person_notify`): de deurbel en comfortmeldingen gaan naar wie thuis is, alarmen naar iedereen. Optioneel een **avondoverzicht** (`digest_time`) voor niet-dringende meldingen.

## 0.5.3 — Slimmere camerameldingen

- Melding toont altijd het actuele camerabeeld (cachebuster op de snapshot-URL).
- Meldingstekst is één korte zin over wie/wat er gebeurt, zonder beschrijving van straat of huizen.
- Geen dubbele detecties meer voor de streams van één camera (Duidelijk/Vloeiend); titel zonder streamnaam.
- Categorie wordt ook herkend als het model een regelovergang na het eerste woord zet; voorbijgangers worden daardoor weer alleen gelogd en niet gepusht.
- Geparkeerde of stilstaande auto's tellen als "niets".
- Iemand bij de voordeur wordt pas gemeld als die na 45 seconden nog staat zonder aan te bellen ("blijft rondhangen").
- Persoon bij een privé-camera (achtertuin) terwijl het alarm aan staat: kritieke melding, ook in stille uren. Iets verdachts met alarm aan is ook kritiek.
- Nieuwe instelling **Camera's → Wat wil je per camera horen?**: per camera Automatisch, Voordeur, Privé, Straat (alleen verdacht) of Negeren. Automatisch kiest Privé bij namen als achtertuin/tuin/schuur.

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
