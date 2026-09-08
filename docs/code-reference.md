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
13. [`akeneo_mapping.py` — PIM-Name](#13-akeneo_mappingpy--pim-name)
14. [`xml_builder.py` — DHL-XML](#14-xml_builderpy--dhl-xml)
15. [`pipeline.py` — Orchestrierung](#15-pipelinepy--orchestrierung)
16. [`cli.py` — Kommandozeile](#16-clipy--kommandozeile)
17. [`web.py` — Manueller Web-Trigger](#17-webpy--manueller-web-trigger)
18. [`notifications.py` — Report-Mail](#18-notificationspy--report-mail)
19. [`logging_setup.py` — Logging](#19-logging_setuppy--logging)
20. [Tests](#20-tests)
21. [Betrieb & Ausführung](#21-betrieb--ausführung)

---

## 1. Projektüberblick

`dhl2mh` ist eine Python-Portierung eines ehemaligen C#-Workflows. Sie führt pro
Aufruf **einen** Durchlauf aus (für Cron gedacht):

> Plenty-Aufträge holen → auf Domain-Modell mappen → mit Shopware anreichern →
> filtern → Produktnamen aus dem Akeneo-PIM holen → Aufträge ohne Modellnummer
> aussortieren → Services auflösen → DHL-DeliverIT-XML bauen & hochladen → auf
> Labels warten → Tracking-Nummer nach Plenty zurückschreiben → Report-Mail für
> übersprungene Aufträge.

**Tech-Stack:** Python ≥ 3.12, `httpx` (async HTTP), `pydantic` / `pydantic-settings`
(Modelle & Config), `typer` (CLI), `lxml` (XML), `structlog` (Logging),
`pytest` / `respx` (Tests). Optional (`.[web]`): `fastapi` + `uvicorn` für den
manuellen Web-Trigger.

**Einstiegspunkte:** `dhl2mh` (Konsolen-Script) bzw. `python -m dhl2mh` → `cli.py`.

---

## 2. Architektur & Datenfluss

Der gesamte Lauf ist async und nutzt je einen Client pro Workflow
(`async with`). Reihenfolge in `pipeline.run_pipeline`:

```
PlentyClient.iter_orders ─► map_order ─► _enrich_from_shopware_order
   ─► require_service_former_parent_ids ─► filter_orders
   ─► _enrich_from_shopware_product ─► _enrich_from_akeneo
   ─► _apply_second_choice ─► require_model_names ─► resolve_orders
   ─► OrderXmlBuilder.build ─► DhlClient.upload_order_xml (?ack=true)
   ─► (warten) ─► DhlClient.get_labels ─► DhlClient.fetch_acknowledgements
   ─► PlentyClient.update_package ─► send_skipped_orders_report
```

Vier externe Systeme:

| System | Client | Auth |
|--------|--------|------|
| PlentyMarkets (REST) | `PlentyClient` | Bearer-Token (`/rest/login`), 401-Retry |
| Shopware 6 (Admin-API) | `ShopwareClient` | OAuth `client_credentials`, Token-TTL |
| Akeneo PIM (2 Instanzen: MK, ML) | `AkeneoClient` | OAuth `password`-Grant + Basic, Token-TTL |
| DHL DeliverIT (DSI/it4logistics) | `DhlClient` | Basic-Auth mit SHA1-Passwort-Hash |

Akeneo ist das einzige **optionale** System: fehlen die `AKENEO*`-Variablen oder
fällt das PIM aus, läuft der Workflow mit dem Shopware-Namen weiter.

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
├── shopware_mapping.py  # former_parent, Festwasser, Fallback-Name, Pflichtfeld-Skip
├── akeneo_mapping.py    # ProductName aus PIM-modell + Farbe
├── xml_builder.py       # DHL-DeliverIT-XML
├── web.py               # optionaler manueller Web-Trigger (FastAPI)
├── notifications.py     # SMTP-Report-Mail
├── logging_setup.py     # structlog-Konfiguration
└── clients/
    ├── plenty.py
    ├── shopware.py
    ├── akeneo.py
    └── dhl.py
tests/                   # pytest-Suite (+ fixtures/)
docs/                    # diese Dokumentation
deploy/                  # dhl2mh-web.service (systemd-Unit für den Web-Trigger)
```

---

## 4. `config.py` — Konfiguration

Pydantic-Settings, geladen aus `.env` (verschachtelt mit Trenner `__`).

- **`Settings(BaseSettings)`** — Top-Level:
  `app_env` (`dev`|`prod`), `report_recipient_email`, sowie die verschachtelten
  Blöcke `plenty`, `shopware`, `dhl`, `smtp`, `web`, `akeneo`, `akeneomk`,
  `akeneoml`.
- **Nested-Modelle:** `PlentySettings` (username/password/base_url),
  `ShopwareSettings` (client_id/client_secret/base_url),
  `DhlSettings` (uat_/prod_ username/password/base_url, `label_wait_seconds=180`,
  `uat_/prod_sender_partner_id` = `1`/`3`), `SmtpSettings`,
  `WebSettings` (username/password/secret_key — leer = Web-Trigger deaktiviert,
  siehe Abschnitt 17),
  `AkeneoSettings` (username/password — von **beiden** PIM-Instanzen geteilt) und
  je Instanz `AkeneoInstanceSettings` (base_url/client_id/secret).
- **`akeneo_instances`** — die konfigurierten PIM-Instanzen in Abfragereihenfolge
  (`MK`, dann `ML`). Leer, wenn die geteilten Zugangsdaten fehlen oder keine
  Instanz vollständig konfiguriert ist → die PIM-Anreicherung entfällt
  ersatzlos und der Shopware-Name bleibt stehen. Damit läuft eine Installation
  ohne `AKENEO*`-Variablen unverändert weiter.
- **Umgebungs-Properties:** `dhl_username`, `dhl_password`, `dhl_base_url`,
  `is_production` — wählen abhängig von `app_env` zwischen UAT und Prod.
- **`get_settings()`** — gecachter Singleton (liest `.env` beim ersten Aufruf).

`.env`-Schlüssel z. B.: `APP_ENV`, `PLENTY__USERNAME`, `SHOPWARE__CLIENT_ID`,
`DHL__UAT_PASSWORD`, `DHL__LABEL_WAIT_SECONDS`, `SMTP__HOST`,
`AKENEO__USERNAME`, `AKENEOMK__BASE_URL`, `AKENEOML__CLIENT_ID`, …

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
- `SwProductInfo` — flaches `/api/search/product`-Ergebnis: `product_number`,
  `manufacturer_number`, `category_ids`, `tag_ids`, `properties`;
  `color(group_id)` liefert den Namen der Farb-Property. Basis für Kategorien,
  den *Fallback*-Namen (den primären `ProductName` liefert Akeneo, siehe 5.2b)
  und die B-Ware-Erkennung über `tag_ids` — die flache Antwort liefert `tagIds`
  ohne angeforderte Assoziation mit.
- `SwOrderLineItem` — `type`, `label`, `referenced_id`, `product_id`, `quantity`,
  `payload`, `product`. `quantity` ist die Menge **dieses** Line-Items — beim
  1:n-Split (Abschnitt 12) wird sie zur Menge der aufgeteilten Plenty-Position.
- `SwOrder` — `order_number`, `line_items`

### 5.2b Akeneo-DTOs (`/api/rest/v1/products`)

Jeder Attributwert ist eine Liste von `{locale, scope, data}`-Einträgen; die
gelesenen Attribute sind weder lokalisiert noch scoped, die Liste hat also genau
einen Eintrag.

- `AkeneoValue` — `locale`, `scope`, `data`
- `AkeneoProduct` — `identifier`, `values`; **`scalar(attribute)`** liefert den
  ersten nicht-leeren Skalarwert als String (Zahlen werden stringifiziert,
  zusammengesetzte Werte wie Medien/Preise ergeben `None`).
- `AkeneoProductInfo` — aufgelöstes Ergebnis je Variante: `model`, `color`.
  `color` ist bereits das **de_DE-Label**, nicht der Options-Code.

### 5.3 Domain-Modelle (was die Pipeline nutzt)

- **`Address`** — Lieferadresse + `full_name`-Property.
- **`OrderItem`** — eine Position. Enthält u. a. `id` (= itemVariationId),
  `stock_limitation`, `bundle_id` (Property 1021), `former_parent_id`,
  `festwasser`, `has_model_name` (steht `name` eine echte Modellbezeichnung —
  aus Akeneo oder Shopware — gegenüber dem bloßen Plenty-Namen?),
  `second_choice` (Shopware-Tag „B-Ware" → `[ZW]`-Präfix im `ProductName`),
  sowie die im Filter/Resolver befüllten Felder
  `service_ids`, `service_match_codes`, `categories`, `weight_kg`, `volume_cbm`.
  Ein `model_validator` setzt `former_parent_id` per Default auf `bundle_id`.
  `quantity`/`packages` können beim 1:n-Split überschrieben werden (Abschnitt 12).
- **`PlentyOrder`** — Auftrag (`id`, `status_id`, `type_id`, `order_date`,
  `addresses`, `order_items`, `package_number`, `shopware_id`).
- **`SkippedOrder`** — Eintrag für die Report-Mail (`order_id`, `reason`, …).
- **`LabelInfo`** — DHL-Antwort, reduziert auf `order_id`, `order_ident`, `barcode`.
- **`PackageData`** — Push-Payload für Plenty (`package_id`, `package_number`, `package_type`).

---

## 6. Clients

Alle: ein Client pro Lauf, als `async with`, eigener `httpx.AsyncClient`.

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
- **`get_product_info(product_number)`** / **`get_product_infos_bulk(..., concurrency=5)`**
  → `SwProductInfo` je Produkt (`POST /api/search/product`, Filter `productNumber`,
  `associations: {categories, properties}`). Liefert Kategorie-IDs, die Felder
  für den Fallback-Namen (`manufacturerNumber` + Farbe) und `tagIds` (B-Ware) —
  Letztere ohne eigene Assoziation, die flache Antwort führt sie als normales
  Feld. Nicht gefundene Produkte fehlen im Bulk-Ergebnis (Aufrufer behält dann
  die Plenty-Werte).
- **`get_order(order_number)`** → `SwOrder | None`. `POST /api/search/order` mit
  LineItems + Produkt-Properties (siehe Logik-Doku Abschnitt 3). Fehler werden
  **geworfen** (das C#-Original verschluckte sie).

### 6.3 `clients/akeneo.py` — `AkeneoClient` / `AkeneoProductLookup`

Ein `AkeneoClient` **je PIM-Instanz**; beide teilen sich Username/Passwort und
unterscheiden sich nur in Base-URL und Client-Credentials.

- **Auth:** OAuth `password`-Grant (`POST /api/oauth/v1/token`), `client_id`/
  `secret` als HTTP-Basic. Token-TTL 3600 s, proaktiver Refresh mit 60-s-Buffer,
  zusätzlich Refresh bei `401` — dieselbe Mechanik wie im Shopware-Client.
- **`get_product_info(plenty_variation_id)`** → `AkeneoProductInfo | None`.
  `GET /api/rest/v1/products` mit `search` auf `plenty_varianten_id` und
  `attributes=modell,color`. `None`, wenn diese Instanz die Variante nicht kennt.
  > `plenty_varianten_id` ist ein *unique number*-Attribut; Akeneos Zahlenfilter
  > kennt **kein** `IN`, deshalb eine Anfrage pro Artikel statt eines Batches.
- **`get_product_infos_bulk(ids, concurrency=5)`** → `{variation_id: Info}`,
  parallel über ein Semaphore. Nicht gefundene Varianten fehlen im Ergebnis.
- **Farb-Label:** `GET /api/rest/v1/attributes/color/options/{code}` →
  `labels.de_DE`, pro Code für die Lebensdauer des Clients gecacht. Das Attribut
  hat ~500 Optionen, ein Lauf berührt davon nur eine Handvoll. Ein unbekannter
  Code (404) wird unverändert durchgereicht statt verworfen; die Platzhalter aus
  `AKENEO_COLOR_PLACEHOLDER_CODES` gelten als „keine Farbe".
- **`AkeneoProductLookup(settings)`** — Fassade über alle konfigurierten
  Instanzen. Fragt sie der Reihe nach (MK, dann ML) und reicht an die nächste
  Instanz nur weiter, was noch offen ist. Übernommen werden nur Treffer **mit**
  `modell` — ein Eintrag ohne Modell ergibt keinen Namen und bleibt offen.
  `enabled` ist `False`, wenn keine Instanz konfiguriert ist.

### 6.4 `clients/dhl.py` — `DhlClient`

- **Auth:** `Basic base64("USER:SHA1_UPPER_HEX(PW)")` — DHL-Vorgabe, kein
  Security-Design (`_build_basic_auth`).
- **`upload_order_xml(xml_bytes)`** → `POST /transmission/{mandant}?ack=true`
  (`Content-Type: text/xml`). Gibt `list[AckError]` zurück — **leer = angenommen**.
  Wirft nur bei non-2xx; eine *Ablehnung* ist HTTP 200 und damit ein Rückgabewert,
  kein Fehler.
- **`get_labels()`** → `list[LabelInfo]`. Zieht `/transmissionStatus/{mandant}`,
  parst alle `Status` vom Typ `OrderDocument` mit `Document` vom Typ `Label`,
  extrahiert `OrderId/Id` + `OrderIdent` (+ Barcode), und **dedupliziert pro
  `order_id`** (`_dedupe_by_order` — eine Nummer pro Auftrag).
- Unvollständige Einträge (kein `OrderIdent`) werden als `dhl.label_incomplete`
  geloggt und übersprungen.
- **`get_labels_for_order(order_id)`** → derselbe Endpunkt mit
  `?orderId={System}_{Id}`. Leert die Sammelqueue **nicht** — für die gezielte
  Untersuchung eines Auftrags. Der Lauf selbst nutzt ihn nicht.
- **`fetch_acknowledgements(archive_dir)`** → `(Path, list[AckError])`. Zieht
  `/transmissionAcknowledgement/{mandant}` **streamend** mit eigenem Read-Timeout
  (`DHL__ACK_READ_TIMEOUT_SECONDS`, Default 600 s) und schreibt die Rohantwort
  chunkweise nach `archive_dir`, *bevor* geparst wird. Beides folgt daraus, dass
  der Abruf consume-once ist: ein Timeout leert die Queue serverseitig trotzdem.
- **`_parse_ack_errors(xml)`** (statisch, für beide Wege): sammelt jeden
  `AcknowledgementDetails`-Block **mit `ErrorCode`**. Ohne `ErrorCode` ist der
  Block eine Bestätigung — er spiegelt den Auftrag nur zurück und steht immer da.
  Unparsebare Antworten werden als `dhl.ack_unparseable` geloggt und liefern eine
  leere Liste, statt den Lauf zu beenden.

Details zur Semantik: Logik-Doku Abschnitt 12.

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
  **`SERVICE_WHITELIST`** (14 IDs).
- **Auto-Attach:** `HEAVY_LIFT_THRESHOLD_KG=179` (→ `SWG`),
  `VPR_TRIGGER_MATCH_CODES` (`AWS`, `ISEK`, `KF`, `E-AN`, `IS` → `VPR`).
- **`SHOPWARE_PRODUCT_NUMBER_ALIASES`** — Plenty-Variationsnummer → abweichende
  Shopware-`productNumber` (`783172` „Installationsservice – KG" → `783149`).
  Wird nur konsultiert, wenn kein Line-Item die Variationsnummer selbst trägt;
  siehe Logik-Doku Abschnitt 3.2.
- **`HERDE_CATEGORY_IDS`** — Shopware-Kategorien „Herde" → `E-AN`.
- **`WATER_CONNECTION_GROUP_ID`** / **`WATER_CONNECTION_MATCH_CODE="AWS"`**.
- **`COLOR_GROUP_ID`** — Shopware-Property-Group „Farbe" (nur noch für den
  Fallback-Namen).
- **`SECOND_CHOICE_TAG_ID`** (`019745bc…`) / **`SECOND_CHOICE_PREFIX="[ZW]"`** —
  Shopware-Tag „B-Ware" markiert Zweite-Wahl-Artikel; deren `ProductName`
  bekommt das Präfix vorangestellt. Gematcht nach **Tag-Id**, nicht nach Namen:
  der Name ist im Admin umbenennbar, die Id nicht.
- **Akeneo-Attributcodes:** `AKENEO_MODEL_ATTRIBUTE="modell"`,
  `AKENEO_COLOR_ATTRIBUTE="color"`,
  `AKENEO_VARIATION_ID_ATTRIBUTE="plenty_varianten_id"`,
  `AKENEO_LABEL_LOCALE="de_DE"` sowie
  **`AKENEO_COLOR_PLACEHOLDER_CODES`** = `{empty, Nicht_zutreffend}` — Optionen
  mit der Bedeutung „keine Farbe erfasst". Bewusst nach **Code** gematcht: eine
  Heuristik über das Label würde früher oder später eine echte Farbe schlucken.
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

**`require_model_names(orders)`** → `ModelNameResult(passed, skipped)` — das
zweite Tor, läuft **nach** der Produkt-Anreicherung (Pipeline-Schritt 6c):

8. `Artikel ohne Modellnummer in Akeneo und Shopware: {id} ({name})`

Geprüft wird `OrderItem.has_model_name`, das die beiden Anreicherungsschritte
setzen. Ein Artikel ohne Modellnamen aus **beiden** Quellen kippt den **ganzen**
Auftrag — er wird nicht übertragen, sondern gemeldet. Der Plenty-`order_item_name`
zählt nicht als Modellnummer.

---

## 11. `service_resolver.py` — Service-Auflösung

`resolve_orders(orders)` → `ResolveResult(passed, skipped)`; mutiert Artikel in-place.

Pro Bundle (genau 1 Artikel, garantiert durch den Filter):

- sammelt Service-IDs der Bundle-Services,
- fügt **SWG** hinzu, wenn Gewicht > 179 kg,
- mappt jede ID via `map_to_match_codes(..., festwasser=article.festwasser)`,
- fügt **VPR** hinzu, wenn ein Trigger-Code vorhanden ist,
- setzt `service_ids`, `service_match_codes`, `weight_kg` (g→kg),
  `volume_cbm` (aus mm-Maßen).

Unbekannte Service-IDs → Auftrag wird geskippt.

---

## 12. `shopware_mapping.py` — Shopware-Anreicherung

Reine Funktionen (API-entkoppelt, gut testbar):

- **`assign_former_parent_ids(order, sw_order)`** → `FormerParentAssignment(matched, split)`;
  setzt `former_parent_id` aus `dvsnProductOptionFormerParentId`, Match über
  `productNumber == str(id)` (sonst über `SHOPWARE_PRODUCT_NUMBER_ALIASES`),
  überschreibt nur bei vorhandenem Wert. Die Zuordnung ist **1:n**: eine
  Plenty-Position ist das Aggregat aller Line-Items mit ihrer `productNumber`,
  deshalb wird eine Position mit **mehreren** Parents in je eine Position pro
  `former_parent_id` **gesplittet** (`quantity`/`packages` aus den
  Shopware-Mengen). `matched`/`split` landen als `former_parent_matched` /
  `former_parent_split` im Log. Details: Logik-Doku Abschnitt 3.1.
- **`assign_water_connection(order, sw_order)`** → setzt `festwasser` aus der
  Property-Group „Wasseranschluss" (`name` = ja/nein).
- **`product_model_name(info)`** → **Fallback**-`ProductName` aus
  `manufacturerNumber` + Farbe (`COLOR_GROUP_ID`), sonst `None`. Nur wenn
  **beide** vorhanden sind, wird kombiniert. Bewusst **kein** Rückfall auf den
  Plenty-Namen: fehlt hier und bei Akeneo (Abschnitt 13) etwas, wird der
  Auftrag geskippt (`require_model_names`).
- **`is_second_choice(info)`** → `True`, wenn das Shopware-Produkt den Tag
  „B-Ware" trägt (`SECOND_CHOICE_TAG_ID` in `tag_ids`). Einzige Quelle für das
  Merkmal — das PIM kennt das Modell, nicht den Zustand der Ware.
- **`require_service_former_parent_ids(orders)`** → `FormerParentResult`; skippt
  Aufträge, deren echte Services kein `former_parent_id` haben.

Details siehe Logik-Doku (Abschnitte 3–7).

---

## 13. `akeneo_mapping.py` — PIM-Name

- **`akeneo_model_name(info)`** → DHL-`ProductName` aus dem PIM-Attribut
  `modell` + Farb-Label, z. B. `UG 5005-30 Schwarz`, sonst `None`.
  - Ohne `modell` → `None`; der Aufrufer versucht dann Shopware und skippt den
    Auftrag, wenn auch das nichts liefert.
  - Ohne Farbe steht das Modell **allein** — ein Modell ohne Farbe ist ein
    vollwertiger Name, also kein Grund weiterzufallen.

Hintergrund: Shopwares `manufacturerNumber` trägt nur die Artikelnummer
(`1514720`), `modell` die lesbare Bezeichnung (`UG 5005-30`) — bei einer
Stichprobe von 3.000 MK-Produkten unterschieden sich beide in 2.634 Fällen.

---

## 14. `xml_builder.py` — DHL-XML

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

`_build_receiver` setzt `Receiver/PartnerId/Id` auf **`order.id`**, nicht auf
`addr.customer_id`: für DHL ist diese Id das Eindeutigkeitskriterium einer
Empfängeradresse, eine wiederholte Id führt zu `CUSTOMER_ALREADY_EXISTS` (und
damit zu einem stillschweigend verworfenen Auftrag — der Upload antwortet
trotzdem mit HTTP 200). Name und Adresse kommen unverändert aus
`order.addresses[0]`. Hintergrund in der Logik-Doku, Abschnitt 11.2.

---

## 15. `pipeline.py` — Orchestrierung

`run_pipeline(settings=None, *, items_per_page=50, category_concurrency=5,
dry_run=False)` → `PipelineSummary(fetched, uploaded, labels_received,
tracking_pushed, skipped, rejected)`.

Ablauf siehe Abschnitt 2, durchnummeriert in der Logik-Doku (Abschnitt 14):
11 Hauptschritte plus die Zwischenschritte 6b, 6c, 6d und 9b. Besonderheiten:

- **Schritt 3+4 vor dem Filter:** Shopware-Anreicherung (former_parent + Festwasser,
  parallel mit Semaphore) und der Pflichtfeld-Skip laufen **vor** dem Filter,
  damit Filter und Resolver denselben Gruppierungs-Schlüssel sehen.
- **Von DHL abgelehnte Aufträge** (`upload_order_xml` liefert `AckError`s)
  zählen als `rejected`, nicht als `uploaded`, und werden aus
  `missing_label_orders` herausgehalten — sonst stünden sie zweimal in der Mail,
  einmal als Ablehnung und einmal als „ohne Label zurückgekommen".
- **Schritt 9b (`_fetch_acknowledgements`)** zieht die Sammelqueue, best-effort:
  ein Fehler dort wird geloggt, kostet dem Lauf aber nicht die Report-Mail —
  Labels sind zu dem Zeitpunkt bereits zurückgeschrieben. `_new_ack_errors`
  dedupliziert Queue-Einträge gegen die Ablehnungen aus den Uploads dieses Laufs
  über (OrderId, ErrorCode).
- **`dry_run`:** überspringt das Plenty-Rückschreiben (Schritt 10) und die Mail
  (Schritt 11) — der DHL-Upload läuft trotzdem (in Prod also echte Labels!).
- **Schritt 6b (Akeneo) nach Schritt 6 (Shopware):** die PIM-Anreicherung
  überschreibt den in Schritt 6 gesetzten Namen. Aus dieser Reihenfolge ergibt
  sich der Vorrang Akeneo → Shopware von selbst; beide setzen dabei
  `has_model_name`.
- **Schritt 6c (`_apply_second_choice`):** setzt für jeden Artikel mit
  `second_choice` erst den `ProductName` aus der Plenty-Variantennummer
  (`OrderItem.variation_number`) und stellt dann `[ZW] ` voran. Läuft **nach**
  6 und 6b, weil beide Namensquellen `name` komplett überschreiben — früher
  gesetzt wären Name und Präfix wieder weg. Das Flag selbst kommt aus Schritt 6
  (Shopware). Die Variantennummer gewinnt hier bewusst gegen Akeneo: eine
  B-Ware-Variante hat eine eigene Plenty-Variations-Id, die das PIM unter
  `plenty_varianten_id` nicht führt (dort steht die Id des Originalartikels) —
  der Lookup in 6b geht also ins Leere. Fehlt die Variantennummer, bleibt es
  beim Namen aus 6b/6.
- **Schritt 6d (`require_model_names`):** kippt Aufträge, deren Artikel aus
  keiner Quelle eine Modellnummer haben. Muss zwingend nach 6b laufen.
- **Robustheit:** ein fehlschlagender Tracking-Push bricht den Lauf nicht ab;
  ein Mail-Fehler ebenfalls nicht. Auch die PIM-Anreicherung ist
  **best-effort** — ein Fehler wird als `pipeline.akeneo_enrichment_failed`
  geloggt und lässt die Shopware-Namen stehen, statt den Lauf zu kippen. Bei
  PIM-Ausfall verschiebt sich die Last damit auf Schritt 6c: Artikel, für die
  auch Shopware nichts hat, sortieren ihren Auftrag aus.

Helper: `_articles`, `_enrich_from_shopware_order`, `_enrich_from_shopware_product`
(Kategorien + Fallback-Name + `second_choice`), `_enrich_from_akeneo`
(`ProductName`), `_apply_second_choice` (Variantennummer + `[ZW]`),
`_maybe_send_report`.

---

## 16. `cli.py` — Kommandozeile

Typer-App. Ein leerer `@app.callback()` hält die App im Multi-Command-Modus,
damit `run` ein benannter Unterbefehl bleibt (sonst lehnt Typer das `run`-Argument
ab). Befehl:

```
dhl2mh run [--items-per-page N] [--concurrency N] [--log-level LVL] [--dry-run]
```

`run` ruft `run_pipeline` auf und gibt die Summary-Zeile aus.

---

## 17. `web.py` — Manueller Web-Trigger

**Optional** — nur aktiv, wenn `WEB__USERNAME` und `WEB__PASSWORD` gesetzt sind
(leer = reiner Cron-Betrieb). Extra `pip install -e ".[web]"` (FastAPI + uvicorn).

Eine passwortgeschützte Ein-Knopf-Seite, die **denselben** Lauf startet wie der
Cron (`python -m dhl2mh run`) — als **Hintergrundprozess**, nicht in-process.
Ergebnisse kommen weiterhin per Report-Mail; die Seite zeigt nur den Status.

- **`app`** — `FastAPI(docs_url=None, redoc_url=None)`; die Oberfläche ist eine
  einzelne HTML-Konstante (`_PAGE`), kein Template-Verzeichnis.
- **`RunState`** — Single-Slot-State: es ist **genau ein** Lauf gleichzeitig
  erlaubt. `_start_run()` liefert `False`, wenn schon einer läuft. `_drain()`
  streamt die Subprozess-Ausgabe in einen Ringpuffer
  (`OUTPUT_TAIL_LINES = 200`) und hält den Exit-Code fest.
- **Session:** signiertes Cookie `dhl2mh_session` (HMAC über `WEB__SECRET_KEY`,
  ersatzweise aus dem Passwort abgeleitet), TTL 8 h, `Secure`-Flag → nur über
  HTTPS. Credential-Vergleich zeitkonstant (`_credentials_ok`).
- **Routen:** `GET /` (Seite), `GET /status` (JSON: läuft / letztes Ergebnis),
  `POST /login`, `POST /logout`, `POST /trigger` (startet den Lauf).

> ⚠️ Der Web-Lauf ist **nicht** mit dem Cron synchronisiert — nur parallele
> *Web*-Läufe werden verhindert. Unter `APP_ENV=prod` erzeugt jeder Klick einen
> echten Produktivlauf.

Betrieb via `deploy/dhl2mh-web.service` (systemd, lauscht nur auf
`127.0.0.1:8095`, davor ein Reverse-Proxy). Setup-Schritte siehe README.

---

## 18. `notifications.py` — Report-Mail

`send_skipped_orders_report(skipped, settings, *, ack_errors=None, now=None)` —
verschickt eine deutschsprachige Klartext-Mail (SMTP + STARTTLS + Login) an
`REPORT_RECIPIENT_EMAIL`. Body listet pro übersprungenem Auftrag ID, Datum,
Kunde, Artikelzahl und Skip-Grund (`_build_body`).

`ack_errors` (DHL-Ablehnungen) bekommen einen **eigenen Abschnitt** mit OrderId,
`ErrorCode` und DHL-Meldung — ein anderer Fall als ein Skip: der Auftrag hat hier
alles bestanden und DHL hat ihn abgewiesen. Der Betreff nennt beide Zahlen
(`_build_subject`). Verschickt wird, sobald **eine** der beiden Listen gefüllt
ist; sind beide leer, passiert nichts.

---

## 19. `logging_setup.py` — Logging

`setup_logging(level="INFO", *, json=None)` konfiguriert `structlog`.
`json=None` erkennt automatisch: **Console-Renderer** (farbig) auf einem TTY
(lokal), **JSON-Renderer** sonst (Cron/Logfile). Felder: Log-Level + ISO-Timestamp.

---

## 20. Tests

`pytest` (async) mit `respx` für HTTP-Mocks; `tests/fixtures/` enthält reale
Beispiel-Responses. Abdeckung pro Modul:

| Testdatei | Fokus |
|-----------|-------|
| `test_config.py` | Settings/Env |
| `test_models.py` | DTO-Parsing |
| `test_plenty_client.py` / `test_shopware_client.py` / `test_dhl_client.py` | Clients (Auth, Parsing, Dedup, Acknowledgement) |
| `test_akeneo_client.py` | PIM-Client: Auth, Farb-Label + Cache, Platzhalter, MK→ML-Durchreichung |
| `test_mapper.py` | ApiOrder → PlentyOrder, Package-Number |
| `test_bundles.py` | Gruppierung, `is_service` |
| `test_mapping.py` | MatchCodes, Festwasser/AWS |
| `test_filter.py` | Skip-Regeln inkl. Modellnummer-Pflicht |
| `test_service_resolver.py` | Service-Auflösung, Rabatt-Ignorierung |
| `test_shopware_mapping.py` | former_parent (inkl. 1:n-Split + Alias), Festwasser, Fallback-Name, B-Ware-Tag, Pflichtfeld-Skip |
| `test_akeneo_mapping.py` | ProductName aus modell + Farbe, fehlendes modell → `None`, `AkeneoProduct.scalar` |
| `test_xml_builder.py` | DHL-XML |
| `test_notifications.py` | Report-Mail, Abschnitt „Von DHL abgelehnt" |
| `test_pipeline.py` | End-to-End-Smoke + Dry-Run + PIM-Name im XML, PIM-Ausfall, Skip ohne Modellnummer, `[ZW]`-Präfix, DHL-Ablehnungen |
| `test_web.py` | Web-Trigger: Login, Session, Single-Slot-Lauf |

Ausführen: `python -m pytest -q`.

---

## 21. Betrieb & Ausführung

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
