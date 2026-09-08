# DHL2MH — Logik-Dokumentation

Beschreibt die **Geschäftslogik** des Workflows: welche Aufträge wie verarbeitet,
gefiltert, angereichert und in DHL-MatchCodes übersetzt werden, und welche Regeln
zum Überspringen führen. Die technische Code-Referenz steht separat in
[`code-reference.md`](./code-reference.md).

> An den mit `{placeholder}` markierten Stellen Screenshots einfügen.

## Inhalt

1. [Auftragsauswahl](#1-auftragsauswahl)
2. [Mapping Plenty → Domain](#2-mapping-plenty--domain)
3. [`former_parent_id` — Service-zu-Artikel-Zuordnung](#3-former_parent_id--service-zu-artikel-zuordnung)
4. [Festwasseranschluss](#4-festwasseranschluss)
5. [Service vs. Rabatt (Whitelist)](#5-service-vs-rabatt-whitelist)
6. [Bundle-Gruppierung](#6-bundle-gruppierung)
7. [Pflichtfeld-Skip (former_parent)](#7-pflichtfeld-skip-former_parent)
8. [Versandfilter — Skip-Regeln](#8-versandfilter--skip-regeln)
9. [Service-Auflösung & MatchCodes](#9-service-auflösung--matchcodes)
10. [Gewicht & Volumen](#10-gewicht--volumen)
11. [DHL-XML-Ausgabe](#11-dhl-xml-ausgabe)
12. [Label-Rückschreiben](#12-label-rückschreiben)
13. [Dry-Run & Umgebungen](#13-dry-run--umgebungen)
14. [Reihenfolge der Logik im Gesamtlauf](#14-reihenfolge-der-logik-im-gesamtlauf)

---

## 1. Auftragsauswahl

Geladen werden nur Plenty-Aufträge mit **Status `6.1`** (Packtisch) — über die
serverseitige Query in `iter_orders` (`statusId=6.1`, `orderProperty_2=26`,
`with[]=shippingPackages|addresses|orderItems.variation`). Alles Weitere
entscheidet die Pipeline clientseitig.

{placeholder}
*(Screenshot: Plenty-Auftragsliste im Status 6.1)*

---

## 2. Mapping Plenty → Domain

Aus jedem Roh-Auftrag wird ein `PlentyOrder`:

- **Positionen:** nur `typeId == 1` (normale Position) **und `typeId == 2`**
  (Bundle-/Set-Parent, z. B. das Service `783117` → AWS+DPW). Bundle-Komponenten
  (`typeId == 3`, z. B. `783143`/`783147`/`783148`) und Versandkosten (`typeId 6`)
  fallen weg — die Komponenten sind nur die Erfüllungs-Aufschlüsselung des Sets
  und würden sonst doppelte/zusätzliche MatchCodes erzeugen. Dasselbe Service
  eigenständig bestellt kommt als `typeId 1` und bleibt damit erhalten.
  Jede Position bekommt `id = itemVariationId` und `stock_limitation` aus der
  Variation.
- **`stock_limitation`-Bedeutung:** `0/1` = Artikel, `2` = Service **oder Rabatt**
  (siehe Abschnitt 5).
- **`bundle_id`:** Plenty-Item-Property `typeId 1021` — Gruppierungs-/Parent-Schlüssel.
- **`shopware_id`:** Order-Property `typeId 7` (= Shopware-`orderNumber`, z. B.
  `MK89611`). Fehlt bei manuell erstellten Aufträgen.
- **`package_number`:** **erste nicht-leere** `packageNumber` über **alle**
  `shippingPackages` (siehe Abschnitt 8 — wichtig für den Skip bereits versandter
  Aufträge).

{placeholder}
*(Screenshot: Plenty-Auftrag mit Positionen, Property 1021 und shippingPackages)*

---

## 3. `former_parent_id` — Service-zu-Artikel-Zuordnung

`former_parent_id` ist der Schlüssel, über den Serviceleistungen ihrem Artikel
zugeordnet werden. Die Zuordnung steuert, in welchen Artikel die Service-MatchCodes
am Ende im DHL-XML gefaltet werden.

**Quelle & Priorität:**

1. **Plenty-Seed:** startet als `bundle_id` (Property 1021).
2. **Shopware-Override:** wird mit `dvsnProductOptionFormerParentId` aus der
   Shopware-Order überschrieben — **nur wenn vorhanden** (Match über
   `productNumber == Plenty-itemVariationId`).
3. Ist beides leer **und** es gibt echte Services → Auftrag wird geskippt (Abschnitt 7).

| Auftrag | Plenty (1021) | Shopware | Ergebnis |
|---|---|---|---|
| mit `shopware_id` | `1234` | UUID vorhanden | UUID |
| mit `shopware_id` | `1234` | leer / nicht gefunden | `1234` |
| manuell (ohne `shopware_id`) | `1234` | — | `1234` |
| Service-Position | leer | leer | **Skip** |

> Ein befülltes Feld wird **nie** durch ein leeres überschrieben.

Die Shopware-Order kommt aus `POST /api/search/order` (Filter auf `orderNumber`,
LineItems + Produkt-Properties; `Accept: application/json` → flaches Format).

{placeholder}
*(Screenshot: Shopware-LineItem mit `dvsnProductOptionFormerParentId`)*

### 3.1 Dieselbe Serviceleistung für mehrere Artikel (1:n)

Shopware schreibt `dvsnProductOptionFormerParentId` **pro Line-Item**. Wird
dieselbe Serviceleistung für zwei Artikel bestellt, ergibt das **zwei
Line-Items** mit gleicher `productNumber`, aber **unterschiedlicher** parentId —
Plenty aggregiert sie jedoch zu **einer** Position mit `quantity 2`. Die
Zuordnung ist also **1:n**, nicht 1:1.

Deshalb wird eine Position, deren Line-Items auf **verschiedene** Parents
zeigen, wieder **aufgeteilt**: eine Position je `former_parent_id`, Menge
(`quantity`/`packages`) aus den Shopware-Line-Items. Jeder Artikel behält so
seine eigenen Services.

| Shopware-Line-Items | Plenty-Position | Ergebnis |
|---|---|---|
| 1× `783149` → Parent A | `783149`, qty 1 | 1 Position, Parent A |
| 2× `783149` → Parent A, Parent B | `783149`, qty 2 | **2 Positionen**, je qty 1 |

> Ohne den Split würde das letzte Line-Item gewinnen und **ein Artikel verlöre
> stillschweigend seine Services** — ohne Skip und ohne Report-Mail.

Bei nur **einem** Parent bleibt das Verhalten unverändert (die Originalposition
wird lediglich befüllt). Wie viele Positionen gematcht bzw. gesplittet wurden,
steht im Log unter `pipeline.shopware_order_matched`
(`former_parent_matched`, `former_parent_split`).

### 3.2 Abweichende Artikelnummern (Alias)

Normalerweise gilt `productNumber == str(Plenty-Variationsnummer)`. Der
**„Installationsservice – KG"** bricht das: Plenty bucht Variante **`783172`**,
Shopware sendet `productNumber` **`783149`**. Ohne Zuordnung fände die Position
keine parentId und der ganze Auftrag würde geskippt (Abschnitt 7).

`SHOPWARE_PRODUCT_NUMBER_ALIASES` bildet solche Fälle ab. Der Alias greift
**nur dann**, wenn **kein** Line-Item die Variantennummer selbst trägt — ein
direkter Treffer hat immer Vorrang.

---

## 4. Festwasseranschluss

Die Shopware-Property-Group **„Wasseranschluss"**
(`8910dbddf00a4d94998289840033982d`) am Produkt liefert den Wert `name = "ja"`
oder `"nein"`. Bei `"ja"` wird `OrderItem.festwasser = True` gesetzt (Match über
`productNumber`).

Das beeinflusst **nur** den Installationsservice `SERVICE_INSTALL` (783139) —
siehe MatchCode-Tabelle in Abschnitt 9.

{placeholder}
*(Screenshot: Shopware-Admin — Property-Group „Wasseranschluss" am Produkt, Wert ja/nein)*

---

## 5. Service vs. Rabatt (Whitelist)

Nicht jede `stock_limitation == 2`-Position ist eine Serviceleistung. Plenty
führt auch **Rabatte/Nachlässe** als solche Positionen (z. B. „2% Rabatt",
„Deal Weeks – 50 EUR Rabatt", „Nachlass" — IDs wie `787119`, mit **negativem
Preis**).

**Regel:** Eine Position ist nur dann ein Service, wenn ihre ID in der
**`SERVICE_WHITELIST`** (14 IDs, alle `783xxx`) steht.

- Echte Services → werden aufgelöst und in MatchCodes übersetzt.
- Andere `stock==2`-Positionen (Rabatte) → werden **überall ignoriert**: weder
  Artikel noch Service, kein Bundle-Effekt, kein Skip, nicht im XML.

> Es gibt **kein** Sicherheitsnetz für unbekannte Service-IDs — die Whitelist
> gilt als vollständig.

{placeholder}
*(Screenshot: Plenty-Auftrag mit Artikel + Rabattposition (stock_limitation 2, negativer Preis))*

---

## 6. Bundle-Gruppierung

Positionen werden zu **Bundles** gruppiert — Schlüssel ist **`former_parent_id`**.
Positionen ohne `former_parent_id` bilden je eine Einzelgruppe.

Ein gültiges Bundle = **genau ein Artikel** plus null oder mehr echte Services.
Diese Struktur wird vom Filter validiert und vom Resolver vorausgesetzt.

{placeholder}
*(Screenshot: Beispiel-Bundle Artikel + Service mit gemeinsamem former_parent_id)*

---

## 7. Pflichtfeld-Skip (former_parent)

Hat ein Auftrag **echte** Services (Whitelist) und mindestens einer davon **kein**
`former_parent_id` (weder Plenty noch Shopware) → **ganzer Auftrag wird geskippt**.

- Aufträge ohne Service → nicht betroffen.
- Aufträge nur mit Rabattpositionen → nicht betroffen.
- Skip-Grund: `Serviceposition ohne FormerParentId: {id}`.

Läuft **vor** dem Versandfilter, damit `former_parent_id` final ist, bevor
gruppiert/validiert wird.

---

## 8. Versandfilter — Skip-Regeln

Ein Auftrag wird übersprungen, sobald eine dieser Bedingungen zutrifft
(in dieser Reihenfolge):

| # | Grund | Bedingung |
|---|-------|-----------|
| 1 | `PackageNumber vorhanden: …` | bereits eine Tracking-Nummer (= versandt) |
| 2 | `Kein normaler Auftrag (TypeId: …)` | `type_id ∉ {1, 2, 5}` |
| 3 | `Artikel-Bundle (noch nicht unterstützt): …` | Bundle-Parent (Order-Item-`typeId 2`) ist ein **Artikel** (`stock_limitation` 0/1), kein Service-Bundle wie `783117` |
| 4 | `Service-Bundle ohne Artikel` | Bundle hat Service(s), aber keinen Artikel |
| 5 | `Bundle '…' enthält mehrere Artikel` | > 1 Artikel im selben Bundle |
| 6 | `Keine Artikel im Auftrag` | gar kein Artikel |
| 7 | `Artikel ohne Gewichtsangabe: …` | Artikel mit `weight == 0/None` |

### Zweites Tor: Modellnummer-Pflicht

`require_model_names` läuft **später** als die Tabelle oben — erst nach der
Produkt-Anreicherung (Schritte 6 + 6b), weil vorher gar nicht feststeht, ob ein
Artikel einen Modellnamen bekommt.

| # | Grund | Bedingung |
|---|-------|-----------|
| 8 | `Artikel ohne Modellnummer in Akeneo und Shopware: … (…)` | Artikel, für den **weder** Akeneo `modell` **noch** Shopware `manufacturerNumber` + Farbe existiert |

Ein solcher Auftrag wird **nicht** an DHL übertragen. Er landet mit Grund und
Artikelnummer in der Report-Mail und als `pipeline.order_skipped`
(`stage=model_name`) im Log. Der Plenty-`order_item_name` zählt dabei **nicht**
als Modellnummer — er ist eine Produktbeschreibung („Miele K 7347 C
Einbau-Kühlschrank inkl. 5 Jahre Garantie"), keine Modellbezeichnung.

### Wichtig: Package-Number-Erkennung

Plenty legt das Original-Paket unter `shippingPackages[0]` mit **leerer**
`packageNumber` ab; die vergebene Nummer steht auf einem **späteren** Paket.
Deshalb wird die **erste nicht-leere** Nummer über **alle** Pakete gesucht —
sonst würden bereits versandte Aufträge erneut verarbeitet.

{placeholder}
*(Screenshot: Plenty-shippingPackages — leeres Paket [0] + späteres Paket mit Nummer)*

---

## 9. Service-Auflösung & MatchCodes

Pro Bundle (genau 1 Artikel) werden die Service-IDs in DHL-MatchCodes übersetzt
und in den Artikel gefaltet.

**Statische Zuordnungen** (Auszug): `AG→AG`, `AWS+DPW`, `KF+E-AN` (zwei Codes aus
einer ID), `SVG`, `LA`, `DI`, …

**`SERVICE_INSTALL` (783139) — kontextabhängig (AWS hat Vorrang):**

| Bedingung | MatchCode |
|---|---|
| Festwasser **und** Herd | `AWS` |
| nur Festwasser | `AWS` |
| nur Herd (Shopware-Kategorie) | `E-AN` |
| weder noch | `IS` |

**Automatisch angehängt:**

- **`SWG`** (Schwerlast) — wenn das Artikelgewicht **> 179 kg** ist.
- **`VPR`** — wenn ein Trigger-Code (`AWS`, `ISEK`, `KF`, `E-AN`, `IS`) vorhanden
  ist. (D. h. `AWS` aus Festwasser zieht automatisch `VPR` nach.)

{placeholder}
*(Screenshot: DHL-XML-Ausschnitt mit `<Services><MatchCode>`-Blöcken)*

---

## 10. Gewicht & Volumen

- **Gewicht:** `weight_kg = weight_g / 1000` (auf 0,01 gerundet).
- **Volumen:** `volume_cbm = (width × length × height) / 1.000.000.000` (mm³ → m³,
  auf 0,001 gerundet); `0`, wenn ein Maß fehlt.

Diese Werte landen je Artikel im XML (`Weight`, `Volume`).

---

## 11. DHL-XML-Ausgabe

- Es wird **ein `Order`** je Auftrag erzeugt, mit Sender, Empfänger (Lieferadresse)
  und je **Artikel** einem `Items`-Block.
- **`ProductName`** kommt aus dem **Akeneo-PIM**: Attribut `modell` + Farb-Label
  (`color`), z. B. `UG 5005-30 Schwarz`. Grund für den Wechsel: Shopwares
  `manufacturerNumber` trägt nur die numerische Artikelnummer (`1514720`),
  `modell` dagegen die lesbare Modellbezeichnung (`UG 5005-30`) — bei der großen
  Mehrheit der Artikel unterscheiden sich die beiden.
  - **Zuordnung** über das eindeutige Zahl-Attribut `plenty_varianten_id`
    (= Plenty-Variantennummer, derselbe Schlüssel wie Shopwares `productNumber`).
  - **Instanzen** werden der Reihe nach gefragt: erst **MK**
    (`pim.mykitchens.de`), dann **ML** (`pim.mylivings.de`) für alles, was MK
    nicht beantworten konnte.
  - **Farbe:** die Produkt-API liefert nur den Options-Code (`kupfer_rose`); das
    de_DE-Label (`Kupfer Rosé`) wird separat geholt und je Code gecacht. Die
    Platzhalter-Optionen `empty` („keine Angabe") und `Nicht_zutreffend` gelten
    als **keine** Farbe — sie sitzen auf rund 3.300 der ~14.700 MK-Produkte und
    würden sonst als `DKF 1 keine Angabe` bei DHL landen.
  - **Fehlt die Farbe**, bleibt das Modell allein stehen (`UG 5005-30`); ein
    Modell ohne Farbe ist ein vollwertiger Name.
- **Quellenkette** für den `ProductName`:
  1. Akeneo `modell` (+ Farbe)
  2. Shopware `manufacturerNumber` + Farbe (Property-Group „Farbe",
     `COLOR_GROUP_ID`) — nur wenn **beide** vorhanden sind
  3. sonst: **Auftrag wird geskippt** (Skip-Regel 8, Abschnitt 8)

  Der Plenty-`order_item_name` ist **keine** dritte Stufe mehr. Er bleibt zwar
  bis zur Anreicherung im Feld stehen, gilt aber nicht als Modellnummer.
- **Zweite Wahl (`[ZW]`):** Trägt das Shopware-Produkt den Tag **„B-Ware"**
  (`019745bc913a7554aef3d1634e45c2a7`), wird dem `ProductName` das Präfix
  `[ZW] ` vorangestellt — z. B. `[ZW] UG 5005-30 Kupfer Rosé`. Details in
  Abschnitt 11.1.
- Die PIM-Anreicherung selbst ist **best-effort**: Sind die `AKENEO*`-Variablen
  leer oder ist das PIM nicht erreichbar, bricht der Lauf **nicht** ab — es
  greift Stufe 2. Fällt das PIM aus *und* hat Shopware keine
  `manufacturerNumber` + Farbe, werden die betroffenen Aufträge geskippt statt
  mit einem Behelfsnamen verschickt.
- **Services erscheinen nicht** als eigene Items — sie stecken als
  `Services/MatchCode`-Blöcke im jeweiligen Artikel.
- Umgebungsabhängig: `Sender/PartnerId/Id` = `1` (UAT) bzw. `3` (Prod).
- **`Receiver/PartnerId/Id` ist die Plenty-Auftrags-ID**, nicht die Kunden-ID —
  siehe Abschnitt 11.2.

### 11.1 Zweite Wahl — `[ZW]`-Präfix

Zweite-Wahl-Artikel (B-Ware) müssen auf dem Label als solche erkennbar sein.
Woran sie erkannt werden und was passiert:

- **Quelle: ausschließlich Shopware.** Das Produkt trägt dort den Tag
  **„B-Ware"** (Tag-Id `019745bc913a7554aef3d1634e45c2a7`). Gematcht wird über
  die **Tag-Id**, nicht über den Namen — der Name ist im Shopware-Admin
  jederzeit umbenennbar, die Id nicht. Das PIM kennt das Modell, nicht den
  Zustand der Ware, kann diese Frage also nicht beantworten.
- **Kein Zusatz-Request:** Die flache Produkt-Antwort liefert `tagIds` von sich
  aus mit; die bestehende Produkt-Abfrage (Schritt 6) reicht.
- **Eigene Namensquelle:** Für B-Ware-Artikel ist die **Plenty-Variantennummer**
  (`variation.number`) die erste Wahl — sie trägt dort bereits die lesbare
  Modellbezeichnung. Grund: Eine B-Ware-Variante ist eine eigene Plenty-Variante
  mit eigener Variations-Id, und genau diese Id kennt Akeneo nicht (unter
  `plenty_varianten_id` steht die Id des Originalartikels). Der PIM-Lookup aus
  Schritt 6b läuft für solche Artikel also ins Leere und würde auf Shopwares
  `manufacturerNumber` zurückfallen — eine nackte Artikelnummer. Die
  Variantennummer kommt ohne Zusatz-Request mit (`with[]=orderItems.variation`).
  Ist sie leer, bleibt es bei der normalen Reihenfolge Akeneo → Shopware.
- **Wirkung:** Dem fertigen `ProductName` wird `[ZW] ` vorangestellt:

  | Shopware-Tag | Namensquelle | `ProductName` im XML |
  |---|---|---|
  | — | Akeneo | `UG 5005-30 Kupfer Rosé` |
  | B-Ware | Plenty-Variantennummer | `[ZW] UG 5005-30 Kupfer Rosé` |
  | B-Ware | Akeneo (keine Variantennummer) | `[ZW] UG 5005-30 Kupfer Rosé` |
  | B-Ware | Shopware-Fallback | `[ZW] HE517ABW0 Schwarz` |

- **Zeitpunkt:** Name und Präfix werden **nach** beiden Namensquellen gesetzt
  (Schritt 6c, siehe Abschnitt 14). Beide Quellen überschreiben `name`
  vollständig — würde 6c früher laufen, wären Name und Präfix beim nächsten
  Schreibzugriff wieder weg.
- Betrifft nur **Artikel**-Positionen; Services haben keinen `ProductName`.

{placeholder}
*(Screenshot: Shopware-Admin — Produkt mit Tag „B-Ware")*

### 11.2 `Receiver/PartnerId` — pro Auftrag, nie pro Kunde

Für DHL ist die Receiver-`PartnerId` die *global eindeutige Identifikation einer
Empfängeradresse* (DSI-Doku 2.23, Abschnitt 4.1). Wiederholt sie sich, lehnt
DeliverIT den Auftrag ab:

```
ErrorCode:     CUSTOMER_ALREADY_EXISTS
ErrorResponse: Customer [HDE, 4099999] already exists!
```

Deshalb steht dort `order.id` und **nicht** `addr.customer_id`. Die Plenty-
Kontakt-ID ist pro Kunde stabil und wiederholt sich zwangsläufig — besonders bei
**Gewährleistungen** (Typ 5), die immer an den Kunden des Elternauftrags gehen.
Vor der Umstellung bekam deshalb *keine* Gewährleistung ein Label (0 von 23,
Juli–September 2026).

Der Kundenname bleibt unverändert in `<Name>` und `<Name1>`; eindeutig gemacht
wird nur der Identifikator.

Dass das monatelang unbemerkt blieb, liegt am Upload: er liefert HTTP 200 mit
leerem Body, und `transmissionStatus` meldet zu abgelehnten Aufträgen gar
nichts. Die Fehlermeldung steht ausschließlich unter
`GET /transmissionAcknowledgement/{Mandant}`.

---

## 12. Label-Rückschreiben

Nach dem Upload wird `DHL__LABEL_WAIT_SECONDS` (Default 180) gewartet, dann
`transmissionStatus` gezogen.

- Aus jedem `Label`-Document werden `OrderId` + `OrderIdent` extrahiert.
- **Dedup:** genau **eine** Nummer pro Auftrag (es gibt **keine
  Multipaket-Sendungen**) — wiederholte Status-Blöcke führen nicht zu doppeltem
  Rückschreiben.
- Die Nummer wird via `update_package` in den Plenty-Auftrag geschrieben
  (→ beim nächsten Lauf greift dann Skip-Regel 1).

> DHL dedupliziert Uploads serverseitig per `OrderId`; die `transmissionStatus`-
> Antwort ist „consume-once" (nur einmal abrufbar).

{placeholder}
*(Screenshot: Plenty-Auftrag nach Rückschreiben mit Tracking-Nummer)*

---

## 13. Dry-Run & Umgebungen

- **`--dry-run`** überspringt **nur** das Plenty-Rückschreiben und die Report-Mail.
  Der **DHL-Upload läuft trotzdem**.
- **Umgebung** über `APP_ENV`: `dev` → DHL **UAT**, `prod` → DHL **Production**.
  Plenty und Shopware sind **immer** live.

> ⚠️ Gegen `APP_ENV=prod` erzeugt ein Dry-Run **echte** DHL-Labels. Ein echter
> Trockenlauf ist nur in UAT möglich.

| | UAT (`dev`) | Production (`prod`) |
|---|---|---|
| DHL-Endpoint | `deliverit-uat.dhl.com` | `deliverit.dhl.com` |
| Sender-PartnerId | `1` | `3` |

{placeholder}
*(Screenshot: Terminal-Ausgabe eines Laufs mit Schlusszeile `fetched=… uploaded=… …`)*

---

## 14. Reihenfolge der Logik im Gesamtlauf

```
1. Aufträge holen (Status 6.1)
2. Mapping → PlentyOrder (former_parent_id = bundle_id als Seed)
3. Shopware-Anreicherung: former_parent_id-Override + Festwasser
4. Skip: echte Services ohne former_parent_id
5. Filter: Package-Number, Typ, Bundle-Struktur, Gewicht
6. Shopware-Produkt: Kategorien (IS/E-AN) + Fallback-Name (manufacturerNumber + Farbe)
6b. Akeneo-PIM: ProductName aus modell + Farbe (MK, dann ML) — best-effort
6c. Zweite Wahl: Name aus der Variantennummer + "[ZW]"-Praefix fuer Artikel
    mit Shopware-Tag "B-Ware"
6d. Skip: Artikel ohne Modellnummer aus beiden Quellen
7. Service-Auflösung: MatchCodes, SWG/VPR, Gewicht/Volumen
8. XML bauen + zu DHL hochladen
9. Warten, Labels ziehen (dedupliziert)
10. Tracking nach Plenty zurückschreiben   (entfällt bei --dry-run)
11. Report-Mail für übersprungene Aufträge  (entfällt bei --dry-run)
```

> Schritte 3 + 4 laufen bewusst **vor** dem Filter, damit Filter und Resolver
> denselben finalen `former_parent_id` für die Bundle-Gruppierung sehen.
