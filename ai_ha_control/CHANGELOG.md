# Changelog

## 0.6.27

- **Bewegingsmelders zijn geen ramen**: het "Openen"-contact van een bewegingsmelder (sabotage van de behuizing) telt niet meer als open raam of deur. Dat geldt voor het thuisoverzicht, de momentopname, de nachtronde, de inbraakregels en "raam open, verwarming aan". Een contact op hetzelfde apparaat als een bewegings- of aanwezigheidssensor, of met "bewegingsmelder"/"motion"/"pir"/"sabotage" in de naam, wordt overgeslagen.
- **Alle lampen, per ruimte**: de momentopname gaf maar 8 lampen mee, waardoor de agent de rest wegliet. Nu staan alle lampen die aan zijn en alle open ramen/deuren erin, gegroepeerd per ruimte (`lights_on_by_area`, `open_by_area` in `get_home_overview`). De namen uit Home Assistant zijn leidend.
- **Beter "Hoe gaat het thuis?"**: lampen staan "aan" (niet "aangeslagen"), de agent noemt welke lampen en ramen in plaats van "een aantal", en de status van camera's ("idle") wordt niet meer genoemd. Ruimtes zoals Overzicht, Server en Meterkast staan niet meer in de lijst met ruimtes.

## 0.6.26

- **"Hoe vol is de auto?" klopt**: de chat leest nu de laadtoestand van de auto zelf (FordPass "Laadtoestand" in %), nooit de 12V-accu of een batterijsensor van een ander apparaat. Een nieuwe regel "Auto" in de live momentopname geeft laadtoestand, of de auto nu laadt, stekker, bereik en slot. Laadt de laadpaal (Alfen e.d.) met vermogen, dan staat er "nu aan het laden" met het laadvermogen, ook als FordPass nog achterloopt. Zonder laadtoestand-sensor zegt de chat dat het niet bekend is.
- **Zoeken op "accu" of "auto"** vindt nu ook de FordPass-sensoren.

## 0.6.25

- **De auto is geen huisdeur**: sloten en deuren van een auto (FordPass, Tesla, ...) tellen niet meer mee voor "staat open terwijl niemand thuis is" of een mogelijke inbraak. De auto rijdt mee als je weg bent. In de nachtronde staat de auto nog wel apart, als "(auto)".
- **"Niemand thuis" volgt de huismodus**: de vraag om af te sluiten komt alleen als de huismodus (de badge bovenin) ook "weg" of "vakantie" zegt. Zegt je eigen modus-helper of scène "thuis", dan wint die van een tracker die achterloopt. Camerameldingen bij "alleen als je weg bent" gebruiken dezelfde bron.
- **Detectie wordt alleen een incident bij een persoon**: gewone beweging (zoals "Schuur Beweging") zonder persoon op het beeld, of als de camera niet kon kijken, komt alleen in de geschiedenis. Een persoon in een privézone met het alarm aan blijft kritiek.
- **Opgeruimd bij de update**: openstaande incidenten van alleen beweging en de vraag om de auto op slot te doen worden één keer automatisch gesloten.

## 0.6.24

- **"Handmatig aangepast" klopt weer**: alleen wat een bewoner echt zelf deed (app, dashboard, spraak) houdt de agent op afstand. Integraties die zelf iets veranderen (Spotify, de auto op afstand starten, een schijnwerper op zijn eigen sensor, het laden van de auto) tellen niet meer mee; een wandschakelaar alleen bij apparaten die de agent zelf regelt (comfortprofielen) of als hij een actie van de agent terugdraait.
- **Een handmatige aanpassing vervalt zodra het apparaat terug is in de stand van ervoor**: zet je de airco aan en later weer uit, dan staat hij niet meer in de lijst.
- **Samenvatting van de agent**: hij krijgt bij handmatig aangepaste apparaten nu de huidige stand mee, zodat hij niet meer gokt wat de bewoner deed ("kamerlampen aangesloten").

## 0.6.23

- **Temperaturen per ruimte in de chat**: "Graag alle temperaturen" geeft nu per ruimte de juiste sensor (de mediaan van de kamersensoren, de gekozen sensor van een comfortprofiel, anders de thermostaat), met vocht en CO₂ waar die er zijn, plus buiten. Ruimtes zonder sensor worden weggelaten in plaats van "geen temperatuur beschikbaar". Ook `get_home_overview` geeft ze (`room_readings`).
- **Geen inleiding meer over de controle**: zinnen als "De momentopname bevat al de benodigde informatie. Hier is het antwoord nog een keer:" verdwijnen uit het antwoord.

## 0.6.22

Tweede ronde verbeteringen aan bestaande functies, na een review van chat, tools, camera's en de web-UI.

- **"Prima" of "ok" voert niets meer uit**: alleen een duidelijk "ja" bevestigt, en alleen wat in de vorige beurt gevraagd werd. Een oude, nog openstaande vraag (zoals de voordeur van het slot) gaat niet meer mee met een later "ja".
- **Camera's in slaap- en badkamers blijven privé**: een deur- of sabotagemelding stuurt geen beeld meer van een camera met profiel "uit", de agent kijkt er niet zelf in en een melding van de agent hangt er geen beeld van aan. Namen als "Babykamer", "Ouderslaapkamer", "Douche" en "Toilet" worden nu ook herkend.
- **Web-UI op oudere iPhones/iPads** (vóór iOS 16.4) werkt weer: de opmaak van chatantwoorden gebruikte iets dat Safari daar niet kent, waardoor de hele pagina stilviel.
- **Web-UI geeft altijd een foutmelding** als een knop niet lukt; schakelaars springen dan terug en je tekst blijft staan. Verlopen of op de telefoon bevestigde vragen verdwijnen uit de chat. Lange namen schuiven de pagina niet meer opzij op een telefoon.
- **Keuzelijsten tonen alle entiteiten** (was maximaal 500), en blijven niet leeg als je het tabblad opent terwijl de add-on nog opstart.
- **Chat**: na "Nieuw gesprek" komt een laat antwoord niet meer in het nieuwe gesprek; de agent gebruikt alleen de laatste 6 uur van een gesprek als context. Tekst die vóór een tool-aanroep al gestreamd werd, plakt niet meer aan het antwoord vast.
- **Geschiedenis**: een kleine dip in een energiemeter telt niet meer als reset (gaf soms duizenden kWh); "sinds 23:00" in de ochtend betekent gisteravond; tijden van automations staan in je eigen tijdzone; "wat is er gebeurd" toont echte acties in plaats van je eigen vragen.
- **Aanwezigheid nabootsen** weet na een herstart nog welke lampen hij aanzette.
- **Kleinere fixes**: de agent doet niet meer tool-aanroepen dan ingesteld, ook niet in één keer; de noodknop en "nu controleren" geven een nette fout als HA of de agent hapert; een verkeerde Ollama-URL wordt niet meer als "bereikbaar" getoond; pauzehelpers gebruiken de tijdzone van Home Assistant.

## 0.6.21

- **Chatgeschiedenis blijft staan**: na herladen of het opnieuw openen van de app zie je het gesprek weer, met dezelfde opmaak. Met **Nieuw gesprek** begin je opnieuw.

## 0.6.20

- **Chat toont opmaak** in plaats van losse sterretjes: vetgedrukt, cursief, kopjes en (geneste) opsommingen worden netjes weergegeven. Veilig opgebouwd zonder HTML uit het antwoord. Ook de samenvatting gebruikt dit.
- **Rustiger antwoorden**: kort en zonder vetgedrukte woorden in gewone zinnen, en geen opmerkingen meer als "De momentopname beantwoordt de vraag volledig".
- **Meldingen en spraak zonder markdown**: pushmeldingen, de tijdlijn, omroepen op de speakers en Assist-antwoorden krijgen platte tekst (opsommingen als "•", uitgesproken als losse zinnen).

## 0.6.19

Beter gebruik van wat er echt in huis hangt (op basis van een entiteitenexport van ~3700 entiteiten).

- **Kamertemperatuur klopt**: per ruimte de mediaan van de echte kamersensoren. Koelkast-/vriezersensoren, thermostaat-setpoints die als temperatuursensor binnenkomen, dauwpunt, omvormer, laadpaal en ventilatie-unit tellen niet meer mee. Per comfortprofiel kun je ook één vaste temperatuursensor kiezen.
- **Comfort werkt samen met je eigen automatiseringen**: een pauze-helper per profiel (input_boolean aan, timer loopt of input_datetime "gepauzeerd tot" in de toekomst = afblijven), en het profiel waarschuwt als automatiseringen dezelfde thermostaat ook aansturen.
- **Ventilatie**: één centrale ventilatie voor alle ruimtes (`ventilation_entity`). Heeft het huis een ventilatie-unit en is er niets ingesteld, dan geen "zet een raam open"-advies meer elke 3 uur.
- **Zonnescherm** gaat niet uit bij harde wind (`shade_max_wind`); buitentemperatuur kan van een eigen sensor komen (`outdoor_sensor`).
- **Voorverwarmen** ook als iemand een zone als "Voorloop thuiskomst" binnenkomt.
- **Huismodus** volgt je eigen helper (`input_text.actieve_scene` of `mode_entity`); extra aanwezigheid via `presence_extra`.
- **Wasmachine/vaatwasser** melden nog maar één keer (meters die ook per fase meten) en kunnen stil (`appliance_quiet`) als je eigen automatisering het al meldt.
- **Camera's**: mail-bezorg-"camera's", Buienradar en de stofzuigerkaart tellen niet meer als camera; babycam en camera's in slaap- en badkamers staan standaard op negeren; de Reolink-"Foto's"-stream is dezelfde camera.
- **Offline-meldingen** alleen nog voor het huis (niet voor de serverruimte en containers, `offline_ignore_areas`, `offline_house_only`).
- **Energie**: de CO₂-intensiteit wordt niet meer als stroomprijs gezien; zonneprognose in een logische volgorde; actuele net-afname/teruglevering en zonne-opwek in de context; melding bij een zonne-overschot met een tip (wasmachine, vaatwasser, auto laden).
- **Stroomstoring**: een UPS die op de accu gaat geeft een kritieke melding met de resterende tijd, en een melding als de stroom terug is.
- **Trilling of sabotage** op deur- en raamsensoren terwijl het alarm aan staat wordt gemeld; een algemeen "onveilig"-contact is bij een uitgeschakeld alarm een waarschuwing in plaats van een alarm.
- **Auto-slot** heet in meldingen en de nachtronde "(auto)" in plaats van een huisdeur.
- **Afval** van vandaag/morgen in de ochtendsamenvatting; **huisdokter** meldt mislukte rookmelder-/gasmeldertests, dubbele entiteiten en instellingen die je kunt invullen; de **export** heeft een tabblad *Opruimen*.
- Vertalingen van de add-on-opties met een dubbele punt in de tekst waren ongeldige YAML.

## 0.6.18

Alleen verbeteringen aan wat er al was, na een code-review.

- **Deuren en alarm beter afgeschermd**: doelen die in `data` verstopt zaten (`area_id`, `device_id`, `floor_id`, `label_id`, `scene.apply` met `entities`) worden geweigerd, want daarmee werden de beveiligingschecks omzeild. Scènes en groepen tellen als beveiliging als er een slot, alarm of garagedeur in zit. Een actie zonder entiteit, een script op naam en automations met apparaat-acties of ruimte-doelen zijn altijd "bevestigen via telefoon of web" (niet met "ja" via spraak).
- **Controle na een actie** probeert het niet opnieuw als de bewoner het intussen zelf heeft teruggezet of de noodknop aan staat.
- **Ongedaan maken**: schaduwacties (nooit uitgevoerd) kunnen niet meer "teruggedraaid" worden; pauze/afspelen, preset en ventilatorstand worden nu echt hersteld.
- **Comfort**: mislukte instellingen worden de volgende minuut opnieuw geprobeerd; de cv gaat terug naar stand-by als je de airco uitzet; ventilatie loopt niet meer vast na een afgewezen vraag; schemerlampen gaan niet meer net na middernacht aan.
- **Meldingen**: één telefoon met een verouderde notify-service houdt de andere niet meer tegen; het avondoverzicht mist geen items meer als er veel andere gebeurtenissen waren of na een herstart.
- **Rook- en lekmelders** die wakker worden uit "onbereikbaar" en meteen alarm geven, of al alarm gaven bij het opstarten, geven nu wel een melding.
- **Huismodus**: een "Afwezig"-scène telt niet meer als er iemand thuis is (geen inbraakmelding bij thuiskomst zonder scène), en een slaapscène geldt hoogstens 12 uur.
- **Bewakingsregels** schommelen niet meer tussen alarm en "weer normaal" rond de grens (eerst 2 minuten normaal), en houden zich aan de stille uren.
- **Aanwezigheid nabootsen** zet de eigen lampen uit als de simulatie stopt terwijl niemand thuis is.
- **Stabiliteit**: één falend onderdeel van de minuutronde stopt de rest niet meer; de HA-verbinding herstelt sneller na een korte onderbreking; een time-out of fout van Ollama wordt netjes gemeld; ongeldige invoer in de web-UI en de API geeft een foutmelding in plaats van een crash; de noodknop wordt altijd gevolgd, ook als hij niet "zichtbaar" is gemaakt; instellingen werken direct zonder herstart.
- **Integratie**: een ongeldige API-sleutel start nu de herauthenticatie in Home Assistant.

## 0.6.17

- **Zoeken en de Excel-export werkten niet met een echte Home Assistant**: HA zet tegenwoordig `None` in de aliaslijst van elke entiteit ("de eigen naam"). Daardoor liep het zoeken naar entiteiten vast en gaf de export een fout. Die lege aliassen worden nu genegeerd.
- Lukt de export toch niet, dan toont de web-UI de echte foutmelding in plaats van een lege ❌, en één vreemde entiteit breekt de export niet meer.

## 0.6.16

- **Aanwezigheid nabootsen op vakantie** (`presence_simulation`, standaard uit): in de huismodus vakantie gaan 's avonds na zonsondergang in de ruimtes die je normaal gebruikt lampen aan en uit, op basis van de geleerde bewegingsactiviteit per uur (zonder leerdata: woonkamer/keuken 18-23 uur), met wat willekeur in tijd en dagen. Alleen lampen, één per ruimte; na middernacht alles uit. Stopt bij thuiskomst of als *Zelf handelen* uit staat.

## 0.6.15

- **Huisdokter**: maandagochtend één melding (alleen als er iets is) met automatiseringen die een fout hebben, uit staan, al 60 dagen niet liepen of naar niet-bestaande of al dagen onbereikbare apparaten verwijzen; beschikbare updates; apparaten die al 3 dagen onbereikbaar zijn; een bijna volle schijf en lege batterijen. Ook op vraag in de chat ("hoe gezond is mijn Home Assistant?"). Alleen lezen (`health_report`).

## 0.6.14

- **Apparaat klaar**: melding naar wie thuis is als de wasmachine, droger of vaatwasser klaar is, herkend aan een stroommeter met die naam (`appliance_alerts`).
- **Water blijft lopen**: telt een watermeter of doorstroomsensor urenlang zonder pauze, dan komt er een waarschuwing (lekkende kraan of wc). Standaard na 2 uur 's nachts of als je weg bent, overdag na 6 uur (`leak_flow_hours`, 0 = uit).

## 0.6.13

- **Herinneringen en geplande acties** via de chat: "herinner me om 18:00 aan de vuilnisbak", "zet over een uur de verwarming op 18". Bewaard over herstarts, zichtbaar en te annuleren bovenaan het tabblad Meldingen. Een geplande actie gaat op het moment zelf door dezelfde regels: deuren en alarm worden dan nog op je telefoon gevraagd. Stond de add-on op dat moment uit, dan wordt een actie niet alsnog uren later uitgevoerd maar gemeld.

## 0.6.12

- **Nachtronde**: bij het slapengaan één melding met open ramen/deuren, sloten of garage die niet dicht zijn en lampen die nog aan staan, met een knop *Lampen uit*. Sloten en garage alleen als vraag op de telefoon. Niets aan de hand = geen melding (`bedtime_check`).

## 0.6.11

- **Alarm niet gezien**: kritieke meldingen hebben een knop *Gezien*. Zonder reactie na `escalate_minutes` (standaard 5) gaat de melding opnieuw naar iedereen en wordt hij omgeroepen, maximaal 3 keer. Ook *Gezien* in het tabblad Incidenten.
- **Bewakingsregels**: "waarschuw als de vriezer boven -15 graden komt" wordt een vaste regel (entiteit, boven/onder/is, waarde, minimale duur, optioneel alleen in bepaalde huismodi). De add-on controleert hem bij elke wijziging zelf, zonder de AI, en meldt het als het weer normaal is. Aanmaken via de chat (met bevestiging) of in het tabblad Huisregels.
- Voorbeeldknoppen in het tabblad Huisregels lopen op een telefoon niet meer buiten beeld.

## 0.6.10

- **⬇ Excel** opent nu een venster met *Excel downloaden*, *Delen…* (deelmenu van de telefoon, waar dat kan) en *Kopieer als tekst*. In de Home Assistant-app kon het bestand niet worden opgeslagen; de tekst kopiëren en plakken werkt daar wel.

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
