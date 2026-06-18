# DHL2MH — Vollständige Code-Referenz

Modul-für-Modul-Dokumentation des gesamten Codes. Für die fachliche Logik
(Filterregeln, Service-Zuordnung, MatchCodes, former_parent, Festwasser, Whitelist)
siehe ergänzend [`logik-dokumentation.md`](./logik-dokumentation.md).

## Inhalt

1. [Projektüberblick](#1-projektüberblick)
2. [Architektur & Datenfluss](#2-architektur--datenfluss)
3. [Projektstruktur](#3-projektstruktur)
4. [`config.py` — Konfiguration](#4-configpy--konfiguration)
5. [`models.py` — Datenmodelle](#5-modelspy--datenmodelle)
6. [Clients](#6-clients)
7. [`mapper.py` — Plenty → Domain](#7-mapperpy--plenty--domain)
8. [`bundles.py` — Gruppierung](#8-bundlespy--gruppierung)
9. [`mapping.py` — Konstanten & MatchCodes](#9-mappingpy--konstanten--matchcodes)
10. [`filter.py` — Versandfilter](#10-filterpy--versandfilter)
11. [`service_resolver.py` — Service-Auflösung](#11-service_resolverpy--service-auflösung)
12. [`shopware_mapping.py` — Shopware-Anreicherung](#12-shopware_mappingpy--shopware-anreicherung)
13. [`xml_builder.py` — DHL-XML](#13-xml_builderpy--dhl-xml)
14. [`pipeline.py` — Orchestrierung](#14-pipelinepy--orchestrierung)
15. [`cli.py` — Kommandozeile](#15-clipy--kommandozeile)
16. [`notifications.py` — Report-Mail](#16-notificationspy--report-mail)
17. [`logging_setup.py` — Logging](#17-logging_setuppy--logging)
18. [Tests](#18-tests)
19. [Betrieb & Ausführung](#19-betrieb--ausführung)

---

## 1. Projektüberblick

`dhl2mh` ist eine Python-Portierung eines ehemaligen C#-Workflows. Sie führt pro
Aufruf **einen** Durchlauf aus (für Cron gedacht):

> Plenty-Aufträge holen → auf Domain-Modell mappen → mit Shopware anreichern →
> filtern → Services auflösen → DHL-DeliverIT-XML bauen & hochladen → auf Labels
> warten → Tracking-Nummer nach Plenty zurückschreiben → Report-Mail für
> übersprungene Aufträge.

**Tech-Stack:** Python 3.14, `httpx` (async HTTP), `pydantic` / `pydantic-settings`
(Modelle & Config), `typer` (CLI), `lxml` (XML), `structlog` (Logging),
`pytest` / `respx` (Tests).

**Einstiegspunkte:** `dhl2mh` (Konsolen-Script) bzw. `python -m dhl2mh` → `cli.py`.

---

## 2. Architektur & Datenfluss

Der gesamte Lauf ist async und nutzt je einen Client pro Workflow
(`async with`). Reihenfolge in `pipeline.run_pipeline`:

```
PlentyClient.iter_orders ─► map_order ─► _enrich_from_shopware_order
   ─► require_service_former_parent_ids ─► filter_orders
   ─► _enrich_categories ─► resolve_orders
   ─► OrderXmlBuilder.build ─► DhlClient.upload_order_xml
   ─► (warten) ─► DhlClient.get_labels ─► PlentyClient.update_package
   ─► send_skipped_orders_report
```

Drei externe Systeme:

| System | Client | Auth |
|--------|--------|------|
| PlentyMarkets (REST) | `PlentyClient` | Bearer-Token (`/rest/login`), 401-Retry |
| Shopware 6 (Admin-API) | `ShopwareClient` | OAuth `client_credentials`, Token-TTL |
| DHL DeliverIT (DSI/it4logistics) | `DhlClient` | Basic-Auth mit SHA1-Passwort-Hash |

---

## 3. Projektstruktur

```
src/dhl2mh/
├── __main__.py          # python -m dhl2mh → cli.app
├── cli.py               # Typer-CLI: `run` (+ --dry-run)
├── pipeline.py          # Orchestrierung des Gesamtlaufs
├── config.py            # Settings (pydantic-settings, .env)
├── models.py            # API-DTOs, Shopware-DTOs, Domain-Modelle
├── mapper.py            # ApiOrder → PlentyOrder
├── bundles.py           # Gruppierung + is_service
├── mapping.py           # Service-IDs, Whitelist, MatchCodes
├── filter.py            # Pass/Skip-Prädikate
├── service_resolver.py  # Services → MatchCodes, SWG/VPR, Gewicht/Volumen
├── shopware_mapping.py  # former_parent, Festwasser, Pflichtfeld-Skip
├── xml_builder.py       # DHL-DeliverIT-XML
├── notifications.py     # SMTP-Report-Mail
├── logging_setup.py     # structlog-Konfiguration
└── clients/
    ├── plenty.py
    ├── shopware.py
    └── dhl.py
tests/                   # pytest-Suite (+ fixtures/)
docs/                    # diese Dokumentation
```

---

## 4. `config.py` — Konfiguration

Pydantic-Settings, geladen aus `.env` (verschachtelt mit Trenner `__`).

- **`Settings(BaseSettings)`** — Top-Level:
  `app_env` (`dev`|`prod`), `report_recipient_email`, sowie die verschachtelten
  Blöcke `plenty`, `shopware`, `dhl`, `smtp`.
- **Nested-Modelle:** `PlentySettings` (username/password/base_url),
  `ShopwareSettings` (client_id/client_secret/base_url),
  `DhlSettings` (uat_/prod_ username/password/base_url, `label_wait_seconds=180`,
  `uat_/prod_sender_partner_id` = `1`/`3`), `SmtpSettings`.
- **Umgebungs-Properties:** `dhl_username`, `dhl_password`, `dhl_base_url`,
  `is_production` — wählen abhängig von `app_env` zwischen UAT und Prod.
- **`get_settings()`** — gecachter Singleton (liest `.env` beim ersten Aufruf).

`.env`-Schlüssel z. B.: `APP_ENV`, `PLENTY__USERNAME`, `SHOPWARE__CLIENT_ID`,
`DHL__UAT_PASSWORD`, `DHL__LABEL_WAIT_SECONDS`, `SMTP__HOST`, …

---

## 5. `models.py` — Datenmodelle

Drei Gruppen. Alle API-Modelle erben von `_ApiModel`
(`alias_generator=to_camel`, `populate_by_name=True`, `extra="ignore"`).

### 5.1 Plenty-API-DTOs (rohes REST-Format)

`ApiVariation` (Gewicht/Maße — `widthMM` etc. explizit aliased), `ApiProperty`
(`type_id`, `value`), `ApiOrderItem` (`type_id`, `item_variation_id`,
`order_item_name`, `quantity`, `variation`, `properties`), `ApiAddress(+Option)`,
`ApiAddressRelation`, `ApiRelation`, `ApiShippingPackage` (`package_number`),
`ApiOrder` (Top-Level), `ApiOrderPage` (`is_last_page`, `entries`),
`ApiCountry` (`id`, `iso_code2`).

### 5.2 Shopware-DTOs (flaches `Accept: application/json`-Format)

- `SwLineItemPayload` — `product_number`, `dvsn_product_option_former_parent_id`
- `SwPropertyOption` — `name`, `group_id` (Property-Werte, z. B. „Wasseranschluss")
- `SwProduct` — `product_number`, `properties` (null-tolerant via `field_validator`)
- `SwOrderLineItem` — `type`, `label`, `referenced_id`, `product_id`, `payload`, `product`
- `SwOrder` — `order_number`, `line_items`

### 5.3 Domain-Modelle (was die Pipeline nutzt)

- **`Address`** — Lieferadresse + `full_name`-Property.
- **`OrderItem`** — eine Position. Enthält u. a. `id` (= itemVariationId),
  `stock_limitation`, `bundle_id` (Property 1021), `former_parent_id`,
  `festwasser`, sowie die im Filter/Resolver befüllten Felder
  `service_ids`, `service_match_codes`, `categories`, `weight_kg`, `volume_cbm`.
  Ein `model_validator` setzt `former_parent_id` per Default auf `bundle_id`.
- **`PlentyOrder`** — Auftrag (`id`, `status_id`, `type_id`, `order_date`,
  `addresses`, `order_items`, `package_number`, `shopware_id`).
- **`SkippedOrder`** — Eintrag für die Report-Mail (`order_id`, `reason`, …).
- **`LabelInfo`** — DHL-Antwort, reduziert auf `order_id`, `order_ident`, `barcode`.
- **`PackageData`** — Push-Payload für Plenty (`package_id`, `package_number`, `package_type`).

---

## 6. Clients

Alle drei: ein Client pro Lauf, als `async with`, eigener `httpx.AsyncClient`.

### 6.1 `clients/plenty.py` — `PlentyClient`

- **Auth:** `POST /rest/login` → `access_token`; Token lazy, einmaliger Refresh
  bei `401` (`_authed_request`).
- **`get_countries()`** → `{country_id: iso_code2}`.
- **`iter_orders(items_per_page)`** → async Stream über alle Seiten von
  `/rest/orders/search`. Query exakt wie im C#-Original: `statusId=6.1`,
  `orderProperty_2=26`, `with[]=shippingPackages|addresses|orderItems.variation`.
- **`update_package(order_id, package)`** → `POST /rest/orders/{id}/shipping/packages`
  (schreibt die Tracking-Nummer zurück). Wirft bei non-2xx.

### 6.2 `clients/shopware.py` — `ShopwareClient`

- **Auth:** OAuth `client_credentials` (`/api/oauth/token`). Token hat TTL
  (`expires_in`) und wird proaktiv (Buffer 60 s) sowie bei `401` erneuert.
  Header: `Authorization: Bearer …` **und** `sw-access-key`.
- **`get_categories(product_number)`** / **`get_categories_bulk(..., concurrency=5)`**
  → Kategorie-IDs je Produkt (`POST /api/search/product`, Filter `productNumber`).
- **`get_order(order_number)`** → `SwOrder | None`. `POST /api/search/order` mit
  LineItems + Produkt-Properties (siehe Logik-Doku Abschnitt 3). Fehler werden
  **geworfen** (das C#-Original verschluckte sie).

### 6.3 `clients/dhl.py` — `DhlClient`

- **Auth:** `Basic base64("USER:SHA1_UPPER_HEX(PW)")` — DHL-Vorgabe, kein
  Security-Design (`_build_basic_auth`).
- **`upload_order_xml(xml_bytes)`** → `POST /transmission/{mandant}`
  (`Content-Type: text/xml`). Wirft bei non-2xx.
- **`get_labels()`** → `list[LabelInfo]`. Zieht `/transmissionStatus/{mandant}`,
  parst alle `Status` vom Typ `OrderDocument` mit `Document` vom Typ `Label`,
  extrahiert `OrderId/Id` + `OrderIdent` (+ Barcode), und **dedupliziert pro
  `order_id`** (`_dedupe_by_order` — eine Nummer pro Auftrag).
- Unvollständige Einträge (kein `OrderIdent`) werden als `dhl.label_incomplete`
  geloggt und übersprungen.

---

## 7. `mapper.py` — Plenty → Domain

`map_order(api, country_codes)` baut aus `ApiOrder` ein `PlentyOrder`:

- **Adresse:** Lieferadresse über `addressRelations.typeId == 2`,
  Kunde über `relations.relation == "receiver"`, Land via `country_codes`
  (Fallback `"FEHLER"`).
- **Positionen** (`_map_order_items`): `typeId ∈ {1, 2}` (`KEPT_ORDER_ITEM_TYPES`)
  — normale Position **und** Bundle-/Set-Parent (z. B. `783117`). Komponenten
  (`typeId 3`) und Versandkosten (`typeId 6`) fallen weg. `id = itemVariationId`,
  `stock_limitation` aus der Variation, `bundle_id` aus Item-Property
  `typeId 1021` (→ seedet `former_parent_id`), Maße/Gewicht aus der Variation.
- **`shopware_id`:** Order-Property `typeId 7` (= Shopware-`orderNumber`).
- **`package_number`** (`_first_package_number`): **erste nicht-leere** Nummer
  über **alle** `shippingPackages` (Bugfix — Index 0 ist immer leer; siehe
  Logik-Doku Abschnitt 8).

Konstanten: `ADDRESS_RELATION_DELIVERY=2`, `RECEIVER_RELATION="receiver"`,
`ITEM_PROPERTY_BUNDLE_ID=1021`, `ORDER_PROPERTY_SHOPWARE_ID=7`, …

---

## 8. `bundles.py` — Gruppierung

Geteilt von Filter und Resolver.

- **`is_service(item)`** → `stock_limitation == 2` **und** `id ∈ SERVICE_WHITELIST`.
  Nicht-gelistete `stock==2`-Positionen (Rabatte) sind weder Artikel noch Service.
- **`group_by_bundle(items)`** → Gruppen nach **`former_parent_id`**; Positionen
  ohne `former_parent_id` bilden je eine Einzelgruppe (Reihenfolge „first-seen").
- **`split_articles_and_services(group)`** → `(articles, services)` über
  `STOCK_LIMITATION_ARTICLE` bzw. `is_service`.

---

## 9. `mapping.py` — Konstanten & MatchCodes

- **Service-IDs** (`SERVICE_AG`, `SERVICE_INSTALL=783139`, `SERVICE_SWG`, …) und
  **`SERVICE_WHITELIST`** (13 IDs).
- **Auto-Attach:** `HEAVY_LIFT_THRESHOLD_KG=120` (→ `SWG`),
  `VPR_TRIGGER_MATCH_CODES` (`AWS`, `ISEK`, `KF`, `E-AN`, `IS` → `VPR`).
- **`HERDE_CATEGORY_IDS`** — Shopware-Kategorien „Herde" → `E-AN`.
- **`WATER_CONNECTION_GROUP_ID`** / **`WATER_CONNECTION_MATCH_CODE="AWS"`**.
- **`STOCK_LIMITATION_ARTICLE=(0,1)`**, **`STOCK_LIMITATION_SERVICE=2`**.
- **`map_to_match_codes(service_id, category_ids, *, festwasser=False)`** →
  Liste von MatchCodes. Zwei IDs liefern zwei Codes. Für `SERVICE_INSTALL`:
  `AWS` (Festwasser, Vorrang) → `E-AN` (Herde) → `IS`. Unbekannte IDs werfen
  `UnknownServiceIdError` (praktisch tot, da nur Whitelist-IDs ankommen).

---

## 10. `filter.py` — Versandfilter

`filter_orders(orders)` → `FilterResult(passed, skipped)`. Skip-Gründe
(`_why_skip`, in Reihenfolge):

1. `PackageNumber vorhanden: …` (bereits versandt)
2. `Kein normaler Auftrag (TypeId: …)` (`type_id ∉ {1, 2, 5}`, `SHIPPABLE_ORDER_TYPE_IDS`)
3. `Artikel-Bundle (noch nicht unterstützt): …` (`_article_bundle_parent`: Bundle-Parent
   `typeId 2` mit `stock_limitation` 0/1 — Artikel-Bundles werden vorerst geskippt)
4. `Service-Bundle ohne Artikel`
5. `Bundle '…' enthält mehrere Artikel`
6. `Keine Artikel im Auftrag`
7. `Artikel ohne Gewichtsangabe: …`

Reine Prädikate, keine Mutation. Nutzt `group_by_bundle` / `split_articles_and_services`.

---

## 11. `service_resolver.py` — Service-Auflösung

`resolve_orders(orders)` → `ResolveResult(passed, skipped)`; mutiert Artikel in-place.

Pro Bundle (genau 1 Artikel, garantiert durch den Filter):

- sammelt Service-IDs der Bundle-Services,
- fügt **SWG** hinzu, wenn Gewicht > 120 kg,
- mappt jede ID via `map_to_match_codes(..., festwasser=article.festwasser)`,
- fügt **VPR** hinzu, wenn ein Trigger-Code vorhanden ist,
- setzt `service_ids`, `service_match_codes`, `weight_kg` (g→kg),
  `volume_cbm` (aus mm-Maßen).

Unbekannte Service-IDs → Auftrag wird geskippt.

---

## 12. `shopware_mapping.py` — Shopware-Anreicherung

Reine Funktionen (API-entkoppelt, gut testbar):

- **`assign_former_parent_ids(order, sw_order)`** → setzt `former_parent_id` aus
  `dvsnProductOptionFormerParentId`, Match über `productNumber == str(id)`,
  überschreibt nur bei vorhandenem Wert.
- **`assign_water_connection(order, sw_order)`** → setzt `festwasser` aus der
  Property-Group „Wasseranschluss" (`name` = ja/nein).
- **`require_service_former_parent_ids(orders)`** → `FormerParentResult`; skippt
  Aufträge, deren echte Services kein `former_parent_id` haben.

Details siehe Logik-Doku (Abschnitte 3–7).

---

## 13. `xml_builder.py` — DHL-XML

`OrderXmlBuilder` (stateless) baut die DSI/it4logistics-XML
(`build(order) -> bytes`). Konstruktor-Parameter sind die Umgebungs-Schalter
(`sending_party_id`, `sender_partner_id` „1"/„3", `message_structure_version`,
`order_type="LIEF_KK"`, `product_type="ZH"`, `freight_terms="FV"`, `work_unit=3`).

Struktur: `Transmission → Messages → MessageContent → Order` mit `OrderId`,
`Sender` (Supplier), `Receiver` (Customer + Adresse), und je Artikel
(`stock_limitation ∈ {0,1}`) ein `Items`-Block mit `CatalogNr`, `ProductName`,
`Quantity`, `Packages`, `Volume`, `Weight` und je MatchCode einem
`Services`-Block (`MatchCode`, `WorkUnit`). Services selbst werden **nicht** als
eigene Items ausgegeben — sie stecken in `service_match_codes` des Artikels.

---

## 14. `pipeline.py` — Orchestrierung

`run_pipeline(settings=None, *, items_per_page=50, category_concurrency=5,
dry_run=False)` → `PipelineSummary(fetched, uploaded, labels_received,
tracking_pushed, skipped)`.

Die 11 Schritte siehe Abschnitt 2. Besonderheiten:

- **Schritt 3+4 vor dem Filter:** Shopware-Anreicherung (former_parent + Festwasser,
  parallel mit Semaphore) und der Pflichtfeld-Skip laufen **vor** dem Filter,
  damit Filter und Resolver denselben Gruppierungs-Schlüssel sehen.
- **`dry_run`:** überspringt das Plenty-Rückschreiben (Schritt 10) und die Mail
  (Schritt 11) — der DHL-Upload läuft trotzdem (in Prod also echte Labels!).
- **Robustheit:** ein fehlschlagender Tracking-Push bricht den Lauf nicht ab;
  ein Mail-Fehler ebenfalls nicht.

Helper: `_enrich_from_shopware_order`, `_enrich_categories`, `_maybe_send_report`.

---

## 15. `cli.py` — Kommandozeile

Typer-App. Ein leerer `@app.callback()` hält die App im Multi-Command-Modus,
damit `run` ein benannter Unterbefehl bleibt (sonst lehnt Typer das `run`-Argument
ab). Befehl:

```
dhl2mh run [--items-per-page N] [--concurrency N] [--log-level LVL] [--dry-run]
```

`run` ruft `run_pipeline` auf und gibt die Summary-Zeile aus.

---

## 16. `notifications.py` — Report-Mail

`send_skipped_orders_report(skipped, settings, *, now=None)` — verschickt eine
deutschsprachige Klartext-Mail (SMTP + STARTTLS + Login) an
`REPORT_RECIPIENT_EMAIL`. Betreff: „DHL Workflow: N Order(s) übersprungen — …".
Body listet pro Auftrag ID, Datum, Kunde, Artikelzahl und Skip-Grund
(`_build_body`). Bei leerer Liste passiert nichts.

---

## 17. `logging_setup.py` — Logging

`setup_logging(level="INFO", *, json=None)` konfiguriert `structlog`.
`json=None` erkennt automatisch: **Console-Renderer** (farbig) auf einem TTY
(lokal), **JSON-Renderer** sonst (Cron/Logfile). Felder: Log-Level + ISO-Timestamp.

---

## 18. Tests

`pytest` (async) mit `respx` für HTTP-Mocks; `tests/fixtures/` enthält reale
Beispiel-Responses. Abdeckung pro Modul:

| Testdatei | Fokus |
|-----------|-------|
| `test_config.py` | Settings/Env |
| `test_models.py` | DTO-Parsing |
| `test_plenty_client.py` / `test_shopware_client.py` / `test_dhl_client.py` | Clients (Auth, Parsing, Dedup) |
| `test_mapper.py` | ApiOrder → PlentyOrder, Package-Number |
| `test_bundles.py` | Gruppierung, `is_service` |
| `test_mapping.py` | MatchCodes, Festwasser/AWS |
| `test_filter.py` | Skip-Regeln |
| `test_service_resolver.py` | Service-Auflösung, Rabatt-Ignorierung |
| `test_shopware_mapping.py` | former_parent, Festwasser, Pflichtfeld-Skip |
| `test_xml_builder.py` | DHL-XML |
| `test_notifications.py` | Report-Mail |
| `test_pipeline.py` | End-to-End-Smoke + Dry-Run |

Ausführen: `python -m pytest -q`.

---

## 19. Betrieb & Ausführung

```bash
python -m dhl2mh run               # UAT (APP_ENV=dev), voller Lauf
python -m dhl2mh run --dry-run     # ohne Plenty-Rückschreiben + Mail
APP_ENV=prod python -m dhl2mh run  # Production
```

| | UAT (`dev`) | Production (`prod`) |
|---|---|---|
| DHL-Endpoint | `deliverit-uat.dhl.com` | `deliverit.dhl.com` |
| Sender-PartnerId | `1` | `3` |
| Plenty / Shopware | live | live |

> ⚠️ `--dry-run` schützt **nicht** vor dem DHL-Upload. Gegen Production erzeugt
> ein Dry-Run echte Labels. Details: Logik-Doku Abschnitt 13.

Gedacht für **einen** Lauf pro Cron-Invocation.

### Deployment / Update auf dem Server

Der Code liegt auf dem Server als Git-Clone. Nach einem Push nach GitHub holt
man die Änderung per `git pull` auf dem Server ab (als root, im Projektordner):

```bash
cd /var/www/vhosts/moebel-staude.de/dhl2mh.moebel-staude.de/private/dhl2mh
git pull
.venv/bin/pip install -e .   # nur nötig, wenn sich Abhängigkeiten geändert haben
```

Hinweise:

- Der Cronjob nimmt den neuen Stand beim nächsten Lauf automatisch auf — an der
  geplanten Aufgabe muss nichts geändert werden.
- `pip install -e .` ist nur bei geänderten `dependencies` in `pyproject.toml`
  nötig (editable install führt den Code sonst direkt aus).
- Die `.env` ist gitignored und wird von `git pull` nie überschrieben.
