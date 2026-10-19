# AI HA Control

Een **lokale AI (Ollama)** die je huis helpt beheren via Home Assistant. Alles blijft in je eigen netwerk.

## Wat kan het?

- **Chatten met je huis** via *AI Huis* in de zijbalk, of via **Assist/spraak** met de bijbehorende integratie.
  - "Welke ramen staan open?" · "Hoe warm was het gisteren gemiddeld in de woonkamer?"
  - "Zet de thermostaat op 20,5 graden" · "Doe de lampen in de woonkamer op 30% warm wit"
  - "Staat er een pakketje voor de deur?" (camerabeeld wordt bekeken en in de chat getoond)
  - "Hoe vaak is er vandaag iemand bij de voordeur geweest?" · "Hoe laat werd er gisteren aangebeld?" · "Hoe lang stond het raam open?" · "Wanneer kwam Vincent thuis?" (de add-on telt zelf uit de HA-geschiedenis; Duidelijk/Vloeiend-streams tellen als één camera)
  - "Gaat het morgen regenen?" · "Hoeveel stroom hebben we vandaag verbruikt?"
  - "Maak een automatisering die de gang-lamp 's nachts op 10% zet bij beweging"
  - "Onthoud dat ik doordeweeks om 7 uur opsta"
- **Proactief** (zonder dat je iets vraagt):
  - 🔥 Rook, gas, koolmonoxide of lekkage → direct een kritieke melding
  - 📷 Reolink detecteert een persoon/voertuig terwijl niemand thuis is → de AI bekijkt het beeld en stuurt een beschrijving met foto. Bij "niets te zien" geen melding.
  - 🔔 Er wordt aangebeld → melding met beschrijving van wie er staat (ook tijdens stille uren), optioneel uitgesproken op je speakers
  - 🪟 Raam open terwijl de verwarming aan staat
  - 🔓 Slot of garagedeur open terwijl niemand thuis is → melding met knop om het te sluiten
  - ☀️ Ochtendsamenvatting (weer, bijzonderheden van de nacht, lege batterijen, onbereikbare apparaten)
- **Routines leren**: de add-on zoekt elke nacht in het logboek naar handelingen die je vaak rond hetzelfde tijdstip handmatig doet en stelt automatiseringen voor (tabblad *Routines*).
- **Geheugen**: voorkeuren die je vertelt worden onthouden en meegenomen in elk gesprek.

## Autonome agent

De agent bewaakt het huis zelfstandig:

1. **Hartslag**: elke `heartbeat_minutes` (en binnen ~20 s bij iets opvallends) krijgt hij een situatierapport: wat er veranderd is, afwijkingen, open incidenten, geplande controles, huisregels en jouw feedback. Is er niets veranderd, dan slaat hij de beurt over zonder het LLM te gebruiken.
2. Hij beslist: **niets doen**, een **incident** openen of bijwerken (één melding per incident), **zelf handelen** binnen zijn autonomie, of een **controle plannen** ("over 15 minuten kijken of het raam dicht is").
3. **Huismodus** (thuis / weg / slapen / vakantie / gast) komt uit je **modus-scènes** als je die hebt (scènes met namen als *Afwezig, Thuis, Slapen, Gast, Vakantie*; de laatst geactiveerde telt). Anders wordt hij bepaald uit je `person.*`-entiteiten en de slaaptijden.
4. **Wat is normaal**: per sensor en per uur van de dag leert de add-on een basislijn (temperatuur, vocht, stroom, CO₂, fijnstof…). Na ~30 metingen per uur worden grote afwijkingen gemeld. Na twee weken ook beweging of geopende deuren op een uur waarop dat nog nooit gebeurde. Apparaten die tijdens het draaien wegvallen worden gemeld (permanent dode entiteiten niet).
5. **Camerapatrouille** (als je weg bent of slaapt): snapshots worden goedkoop vergeleken met de vorige; alleen bij verschil kijkt het beeldmodel. Voorbijgangers op straat worden genegeerd.

### Wat mag de agent zelf?

Per domein stel je in: **👀 Kijken** (alleen melden), **🕶️ Schaduw** (vastleggen wat hij zou doen, zonder het te doen; beoordeel het met 👍/👎 in het tabblad *Agent*), **❓ Vragen** (melding met knop *Uitvoeren*) of **✅ Zelf doen**. Zelf doen is begrensd:
- alleen als de **noodknop** `input_boolean.ai_autonoom` aan staat (wordt automatisch aangemaakt; ook te bedienen in het tabblad *Agent*);
- alleen **omkeerbare** acties, met een **↩️ Ongedaan maken**-knop in de melding en in de web-UI;
- maximaal `max_autonomous_actions_per_hour` acties per uur en maximaal `max_climate_change` graden temperatuurverschil;
- domeinen in `confirm_domains` vragen altijd eerst, en de agent kan zijn eigen noodknop niet bedienen;
- **deuren, sloten, poorten en het alarm nooit zelf**, ook niet via een schakelaar, knop, scène of script op hetzelfde apparaat of met zo'n naam;
- scènes en scripts alleen als je ze vrijgeeft (`autonomous_scenes`);
- heeft een bewoner een apparaat net zelf aangepast, dan blijft de agent er `override_minutes` vanaf (zie *Handmatig aangepast* in het tabblad *Agent*).

Elke wijziging in het situatierapport staat met **wie** het deed (bewoner, automatisering, agent, AI of het apparaat zelf), op basis van de context die Home Assistant meestuurt. Na een eigen actie controleert de agent na ~45 seconden of het apparaat reageerde.

### Comfort

In het tabblad **🌡️ Comfort** leg je per ruimte vast wat je wilt: temperatuur per huismodus, voorverwarmen voor de thuiskomst, lampen die aangaan bij schemer, ventilatie bij hoog CO₂ (>1000 ppm) of vocht (>75%) en zonwering bij felle zon (>24 °C). Je kunt het ook in de chat zeggen. Vaste regels voeren dit uit (niet het taalmodel), via dezelfde autonomie-instellingen. Ze handelen alleen als de wens verandert (andere huismodus, schemer), dus een handmatige aanpassing wordt niet teruggedraaid. Zet je de thermostaat vaak met de hand anders, dan stelt de add-on voor het profiel aan te passen.

**Airco met cv als backup.** Verwarm je primair met een airco (warmtepomp), kies die als thermostaat en zet onder *Backup-verwarming (cv)* de cv-thermostaat. De cv staat dan op stand-by (standaard 15 °C) en springt pas bij als: de buitentemperatuur onder de ingestelde drempel zakt, de ruimte langer dan de ingestelde tijd (standaard 60 min) meer dan 0,5 °C onder de setpoint blijft, de airco onbereikbaar is of langer dan 15 minuten ontdooit. Eenmaal aan blijft de cv minstens 30 minuten aan en tot de setpoint gehaald is. Koelt de airco (of staat hij op drogen/ventileren), dan gaat de cv direct terug naar stand-by, zodat ze nooit tegen elkaar in werken; zet je de airco zelf uit, dan laat de add-on de cv ook met rust. Delen meerdere ruimtes één cv-thermostaat, dan wint de warmste wens. Het Comfort-tabblad toont per ruimte welke bron nu verwarmt en waarom.

Voorverwarmen gebeurt als iemand onderweg naar huis is (de [proximity-integratie](https://www.home-assistant.io/integrations/proximity/): `sensor.*_direction_of_travel` = towards en de afstand kleiner dan `preheat_distance_km`) of op basis van de geleerde thuiskomsttijd per weekdag.

### Directe beveiligingsregels

Deze regels wachten niet op de AI:
- deur of raam open terwijl iedereen weg is of het alarm aan staat → **kritieke** melding met snapshot (niet vlak na thuiskomst);
- deur open terwijl iedereen slaapt → waarschuwing (👎 = normaal, dan niet meer);
- alarm afgegaan → kritieke melding en omroep;
- lekkage → hoofdkraan dicht (`leak_valve`); rook/gas/CO → alle lampen aan (`emergency_lights`) en ventilatie uit (`emergency_fans_off`).

### Feedback

Elke melding heeft **👍 Nuttig / 👎 Onnodig**. 👎 op een afwijking betekent *dit is normaal*: die wordt gedempt (terug te zetten in het tabblad *Agent*). De agent krijgt je recente 👎's mee en meldt vergelijkbare zaken minder.

### Entiteiten in Home Assistant

| Entiteit | Inhoud |
|----------|--------|
| `sensor.ai_huis_status` | `rustig`, `aandacht` of `alarm` (+ huismodus, aantal incidenten, laatste controle, `last_tick`, `last_agent_run`, `ollama`) |
| `sensor.ai_huismodus` | thuis / weg / slapen / vakantie / gast |
| `binary_sensor.ai_bezorgd` | aan als er een incident met ernst *warning* of *critical* open staat |
| `sensor.ai_laatste_incident` | titel van het laatste incident |
| `input_boolean.ai_autonoom` | noodknop: zelf handelen aan/uit |

Gebruik ze op je dashboard of in eigen automatiseringen (bijv. een lamp rood als `binary_sensor.ai_bezorgd` aan gaat).

Met de **integratie** (zie Assist hieronder) krijg je in plaats daarvan echte entiteiten onder het apparaat *AI HA Control*: huisstatus, huismodus, laatste incident, open incidenten, wacht op bevestiging, AI-reactietijd, bezorgd, add-on-probleem en de schakelaar *Zelf handelen*. Die overleven een herstart van HA, kun je hernoemen en aan een ruimte hangen; de losse `sensor.ai_*` hierboven verdwijnen dan.

Bij elk nieuw of verergerd incident vuurt de add-on het event `ai_ha_control_incident` (`incident_id`, `kind`, `severity`, `title`, `text`, `new`):

```yaml
triggers:
  - trigger: event
    event_type: ai_ha_control_incident
    event_data:
      severity: critical
```

**Dodemansknop**: ligt de add-on zelf plat, dan kan hij dat niet melden. Laat Home Assistant het doen:

```yaml
automation:
  - alias: AI Huis werkt niet meer
    triggers:
      - trigger: template
        value_template: >
          {{ now() - as_datetime(state_attr('sensor.ai_huis_status', 'last_tick'))
             > timedelta(minutes=15) }}
    actions:
      - action: notify.mobile_app_telefoon
        data:
          title: AI Huis reageert niet
          message: De add-on heeft al 15 minuten niet rondgekeken.
```

Is alleen de AI-server (Ollama) weg, dan meldt de add-on dat zelf na `llm_down_minutes`.

### Nachtronde

Gaat het huis naar de slaapmodus (slaapscène of begin van `sleep_hours`), dan krijg je één melding met ramen en deuren die open staan, sloten en garage die niet dicht zijn en lampen die nog aan staan, met een knop *Lampen uit*. Voor elk slot en de garage komt een aparte vraag op je telefoon; de agent doet dat nooit zelf. Staat alles goed, dan krijg je niets.

### Bewakingsregels

Voor een drempel op één meetwaarde of status, bijvoorbeeld "waarschuw als de vriezer boven -15 graden komt" of "meld het als de achterdeur langer dan 10 minuten open staat". Vraag het in de chat (je bevestigt de regel) of maak hem in het tabblad *Huisregels*. De add-on controleert de regel zelf bij elke wijziging en elke minuut, zonder de AI, opent één incident en meldt het als de waarde weer normaal is. Ernst *kritiek* gebruikt dezelfde escalatie als een alarm.

### Alarm niet gezien

Kritieke meldingen (inbraak, rook, gas, lekkage, alarm afgegaan) hebben een knop **👁 Gezien**. Drukt niemand daarop binnen `escalate_minutes`, dan gaat de melding opnieuw naar alle bewoners en wordt hij omgeroepen op de speakers, maximaal 3 keer. Het stopt bij *Gezien*, 👍/👎, het afsluiten van het incident of *Gezien* in het tabblad Incidenten.

## Veiligheid

- Start standaard in **testmodus** (`dry_run: true`): de AI vertelt wat hij zou doen, maar voert niets uit. Controleer het *Actielog* en zet testmodus daarna uit.
- Sloten, alarm, rolluiken/garagedeuren, kranen en **nieuwe automatiseringen** vragen altijd om bevestiging: in de web-UI, met een knop op je telefoon of door "ja" te zeggen. Alles wat deuren of het alarm raakt kan **niet** via spraak worden bevestigd.
- De API-sleutel (voor de Assist-integratie) geeft alleen toegang tot het gesprek; instellingen en de noodknop alleen via het HA-paneel.
- HA herstarten/stoppen, shell-commando's, add-ons beheren en de recorder zijn geblokkeerd.
- De add-on draait onder een eigen **AppArmor-profiel**: alleen `/data` is schrijfbaar, de code is alleen-lezen en de HA-config, back-ups, media en gedeelde mappen zijn afgeschermd.
- `/api/health` controleert de verbinding met HA en de interne planner; de Supervisor-watchdog herstart de add-on bij problemen.
- HA-back-ups bevatten een consistente kopie van de database (gemaakt vlak voor de back-up), zonder camera-snapshots.
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

**Sneller**: zet in de spraakassistent **"Prefer handling commands locally"** (*Commando's lokaal afhandelen*) aan. Simpele commando's als "doe de keukenlamp aan" doet HA dan zelf in een fractie van een seconde; alleen vragen en lastige opdrachten gaan naar de AI. Antwoorden van de AI worden gestreamd, en de ruimte van de spraaksatelliet gaat mee ("doe het licht aan" = licht in die ruimte).

Vanaf versie 0.6.2 heeft de integratie Home Assistant 2025.6 of nieuwer nodig.

## Opties

Alle opties zijn ook te bewerken in **AI Huis → ⚙️ Instellingen**: gegroepeerd, met uitleg, keuzelijsten met je eigen modellen, telefoons en speakers, en direct toegepast zonder herstart. Wijzigingen worden ook in de add-on-configuratie van Home Assistant opgeslagen.

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
| `tts_entity` | – | Bijv. `tts.piper` of `tts.google_translate_nl_nl`; nodig voor aankondigingen |
| `announce_players` | – | Speakers (`media_player.*`) waarop deurbel en alarmen worden uitgesproken |
| `autonomous` | `true` | Autonome agent aan/uit |
| `autonomy` | zie boven | Per domein `observe`, `shadow`, `ask` of `act` (sloten en alarm hooguit `ask`) |
| `kill_switch` | `input_boolean.ai_autonoom` | Noodknop (wordt aangemaakt) |
| `max_climate_change` | `2.0` | Max. temperatuurwijziging die de agent zelf mag doen |
| `max_autonomous_actions_per_hour` | `10` | Max. eigen acties per uur |
| `triage_model` | – | Klein snel model voor de hartslag, bijv. `qwen3:4b-instruct` (leeg = taalmodel) |
| `heartbeat_minutes` | `10` | Hoe vaak de agent rondkijkt |
| `sleep_hours` | `23:00-07:00` | Huismodus 'slapen' (als er geen modus-scènes zijn) |
| `camera_patrol` | `away` | `off`, `away` (weg/slapen) of `always` |
| `patrol_minutes` | `15` | Interval van de patrouille |
| `snapshot_retention_days` | `7` | Hoe lang snapshots van incidenten bewaard blijven |
| `autonomous_scenes` | – | Scènes/scripts die de agent zelf mag starten |
| `override_minutes` | `120` | Na een handmatige aanpassing blijft de agent zo lang van dat apparaat af |
| `comfort` | `true` | Comfortprofielen uitvoeren |
| `preheat_distance_km` | `15` | Voorverwarmen als iemand onderweg is en dichterbij is dan dit |
| `leak_valve` | – | Kraan (`valve.*` of `switch.*`) die bij lekkage dicht gaat |
| `emergency_lights` | `true` | Alle lampen aan bij rook/gas/CO |
| `emergency_fans_off` | – | Ventilatie die bij rook uit gaat |
| `person_notify` | – | Telefoon per persoon: `person.vincent=notify.mobile_app_vincent`. Deurbel/comfort naar wie thuis is, alarmen naar iedereen |
| `digest_time` | – | Avondoverzicht van niet-dringende meldingen, bijv. `20:00` |
| `llm_down_minutes` | `30` | Melding als de AI-server zo lang onbereikbaar is |
| `bedtime_check` | `true` | Nachtronde: bij het slapengaan één melding met wat nog open of aan staat |
| `escalate_minutes` | `5` | Kritieke melding zonder *Gezien* na zoveel minuten opnieuw naar iedereen en omroepen (max. 3 keer, 0 = uit) |
| `calendar_context` | `true` | Agenda in de ochtendsamenvatting; lopende afspraak met "vakantie"/"gasten" zet de huismodus |
| `energy_context` | `true` | Stroomprijs, goedkoopste tijdvak en zonneverwachting in de samenvatting en de chat |
| `api_key` | automatisch | Sleutel voor de Assist-integratie (alleen het gesprek) |

"Niemand thuis" wordt bepaald aan de hand van je `person.*`-entiteiten. Zonder personen worden afwezigheidsregels overgeslagen.

## Reolink

Gebruik de officiële Reolink-integratie. De add-on koppelt detectiesensoren (persoon, voertuig, dier, bezoeker) automatisch aan de camera van hetzelfde apparaat en gebruikt de *clear*-stream voor de beste beeldkwaliteit.

## Entiteiten delen

De knop **⬇ Excel** bovenaan de web-UI downloadt alle entiteiten die de add-on ziet (naam, ruimte, apparaat, status, eigenschappen) en een tweede blad met hoe de detectiesensoren per camera gegroepeerd zijn. Handig om mee te sturen als een vraag verkeerd beantwoord wordt.

## Modellen vergelijken

Met `scripts/benchmark.py` in de repository test je modellen op een gesimuleerd huis (er wordt niets in HA uitgevoerd), inclusief een dag geschiedenis voor vragen als "hoe vaak", "wanneer" en "hoe lang":

```bash
python scripts/benchmark.py --ollama http://<ip-homeserver>:11434 --models qwen3:8b qwen3:4b
```

Met `scripts/replay.py` speel je echte situatierapporten van je eigen huis opnieuw af (kopieer `ai_ha_control.db` uit de add-on). Je 👍/👎 op controles in het tabblad *Agent* geldt als beoordeling:

```bash
python scripts/replay.py --db ai_ha_control.db --ollama http://<ip-homeserver>:11434 --models qwen3:8b qwen3:4b
```
