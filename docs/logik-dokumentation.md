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
**`SERVICE_WHITELIST`** (13 IDs, alle `783xxx`) steht.

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
| 3 | `Service-Bundle ohne Artikel` | Bundle hat Service(s), aber keinen Artikel |
| 4 | `Bundle '…' enthält mehrere Artikel` | > 1 Artikel im selben Bundle |
| 5 | `Keine Artikel im Auftrag` | gar kein Artikel |
| 6 | `Artikel ohne Gewichtsangabe: …` | Artikel mit `weight == 0/None` |

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

- **`SWG`** (Schwerlast) — wenn das Artikelgewicht **> 120 kg** ist.
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
- **Services erscheinen nicht** als eigene Items — sie stecken als
  `Services/MatchCode`-Blöcke im jeweiligen Artikel.
- Umgebungsabhängig: `Sender/PartnerId/Id` = `1` (UAT) bzw. `3` (Prod).

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
6. Kategorien aus Shopware (für IS/E-AN-Entscheidung)
7. Service-Auflösung: MatchCodes, SWG/VPR, Gewicht/Volumen
8. XML bauen + zu DHL hochladen
9. Warten, Labels ziehen (dedupliziert)
10. Tracking nach Plenty zurückschreiben   (entfällt bei --dry-run)
11. Report-Mail für übersprungene Aufträge  (entfällt bei --dry-run)
```

> Schritte 3 + 4 laufen bewusst **vor** dem Filter, damit Filter und Resolver
> denselben finalen `former_parent_id` für die Bundle-Gruppierung sehen.
