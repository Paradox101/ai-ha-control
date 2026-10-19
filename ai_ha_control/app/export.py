"""Excel export of everything the add-on can see, to share when something is answered wrong.

A minimal .xlsx (Office Open XML) written with the standard library: no extra package in the image.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from .ha.index import EntityIndex
from .tools.history import KIND_LABELS, detection_places, sensor_kind

_LOGGER = logging.getLogger(__name__)

ENTITY_COLUMNS = ["entity_id", "naam", "domein", "ruimte", "apparaat", "device_class", "status",
                  "status (leesbaar)", "eenheid", "laatst gewijzigd", "aliassen", "eigenschappen"]
PLACE_COLUMNS = ["plek", "camera (beste stream)", "sensor", "naam", "soort detectie", "status"]
CLEANUP_COLUMNS = ["entity_id", "naam", "reden", "ruimte", "apparaat", "status", "laatst gewijzigd"]
DEAD_DAYS = 30


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def entity_rows(index: EntityIndex) -> list[list[Any]]:
    rows = []
    for ent in sorted(index.entities.values(), key=lambda e: e.entity_id):
        try:
            attrs = ent.useful_attributes()
            attrs.pop("device_class", None)
            rows.append([_text(v) for v in (
                ent.entity_id, ent.name, ent.domain, ent.area, ent.device, ent.device_class, ent.state,
                ent.readable_state, ent.unit, ent.last_changed, ", ".join(_text(a) for a in ent.aliases if a),
                ", ".join(f"{k}={v}" for k, v in attrs.items()))])
        except Exception as err:  # noqa: BLE001 - one odd entity must not break the whole export
            _LOGGER.warning("Export: skipping %s: %s", ent.entity_id, err)
            rows.append([ent.entity_id, f"(fout bij exporteren: {err})"])
    return rows


def place_rows(index: EntityIndex) -> list[list[Any]]:
    """How the add-on groups detection sensors per physical camera (what get_detections counts)."""
    rows = []
    try:
        places = sorted(detection_places(index).values(), key=lambda p: _text(p.label))
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Export: camera grouping failed: %s", err)
        return [["(fout bij exporteren)", _text(err)]]
    for place in places:
        for sensor in sorted(place.sensors, key=lambda s: s.entity_id):
            rows.append([_text(v) for v in (
                place.label, place.camera.entity_id if place.camera else "", sensor.entity_id, sensor.name,
                KIND_LABELS[sensor_kind(index, sensor)], sensor.readable_state)])
    return rows


def cleanup_rows(index: EntityIndex) -> list[list[Any]]:
    """Candidates to remove in Home Assistant: old copies (_2/_3) and entities that have been dead for a month."""
    from .house import duplicates
    from .watcher.baseline import minutes_since

    rows, seen = [], set()
    for original, dup in duplicates(index):
        seen.add(dup.entity_id)
        rows.append([_text(v) for v in (dup.entity_id, dup.name, f"kopie van {original.entity_id}", dup.area,
                                        dup.device, dup.state, dup.last_changed)])
    for ent in sorted(index.entities.values(), key=lambda e: e.entity_id):
        if ent.entity_id in seen or ent.state not in ("unavailable", "unknown") or ent.domain == "automation":
            continue
        days = minutes_since(ent.last_changed) / 1440
        if days >= DEAD_DAYS:
            rows.append([_text(v) for v in (ent.entity_id, ent.name, f"al {int(days)} dagen {ent.state}", ent.area,
                                            ent.device, ent.state, ent.last_changed)])
    return rows


def _column(n: int) -> str:
    name = ""
    while n >= 0:
        name = chr(65 + n % 26) + name
        n = n // 26 - 1
    return name


def _sheet(header: list[str], rows: list[list[Any]]) -> str:
    lines = []
    for r, row in enumerate([header, *rows], start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            style = ' s="1"' if r == 1 else ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                text = escape("".join(ch for ch in str(value) if ch >= " " or ch in "\t\n"))
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>')
        lines.append(f'<row r="{r}">{"".join(cells)}</row>')
    last = f"{_column(len(header) - 1)}{len(rows) + 1}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            f'<cols><col min="1" max="{len(header)}" width="28" customWidth="1"/></cols>'
            f'<sheetData>{"".join(lines)}</sheetData><autoFilter ref="A1:{last}"/></worksheet>')


def workbook(sheets: list[tuple[str, list[str], list[list[Any]]]]) -> bytes:
    ns = "http://schemas.openxmlformats.org"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{ns}/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(sheets) + 1))
            + "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{ns}/spreadsheetml/2006/main" xmlns:r="{ns}/officeDocument/2006/relationships">'
            "<sheets>" + "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                                 for i, (name, _, _) in enumerate(sheets, start=1))
            + "</sheets>"
            + "".join(f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                      f"'{escape(name)}'!$A$1:${_column(len(header) - 1)}${len(rows) + 1}</definedName>"
                      "</definedNames>" for i, (name, header, rows) in enumerate(sheets[:1]))
            + "</workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="{ns}/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{ns}/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/></Relationships>'),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{ns}/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'),
    }
    for i, (_, header, rows) in enumerate(sheets, start=1):
        files[f"xl/worksheets/sheet{i}.xml"] = _sheet(header, rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def sheets(index: EntityIndex) -> list[tuple[str, list[str], list[list[Any]]]]:
    return [("Entiteiten", ENTITY_COLUMNS, entity_rows(index)),
            ("Camera's en detectie", PLACE_COLUMNS, place_rows(index)),
            ("Opruimen", CLEANUP_COLUMNS, cleanup_rows(index))]


def entities_xlsx(index: EntityIndex) -> bytes:
    return workbook(sheets(index))


def entities_text(index: EntityIndex) -> str:
    """The same as tab-separated text: copy/paste works where the app's web view cannot download files."""
    def line(values: list[Any]) -> str:
        return "\t".join(" ".join(str(v).split()) for v in values)

    parts = []
    for name, header, rows in sheets(index):
        parts.append("\n".join([f"## {name} ({len(rows)})", line(header), *map(line, rows)]))
    return "\n\n".join(parts) + "\n"


def export_filename(now: datetime | None = None, extension: str = "xlsx") -> str:
    return f"ai-huis-entiteiten-{(now or datetime.now()):%Y%m%d-%H%M}.{extension}"
