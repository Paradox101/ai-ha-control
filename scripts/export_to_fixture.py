"""Turn the add-on's Excel entity export into a JSON house for tests_house/.

    python3 scripts/export_to_fixture.py ai-huis-entiteiten.xlsx house.json

The export holds the state, area, device, device_class, unit and the useful attributes of every entity, which is
enough to rebuild the states and the registries the index loads.
"""

import ast
import json
import re
import sys

import openpyxl


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def attributes(text: str) -> dict:
    """"current_temperature=19.9, hvac_modes=['off', 'heat'], fan_mode=auto" -> dict."""
    result = {}
    for key, value in re.findall(r"(\w+)=(\[[^\]]*\]|[^,]*)", text or ""):
        try:
            result[key] = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            result[key] = value.strip()
    return result


def convert(path: str) -> dict:
    sheet = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    head = rows[0]
    states, entities, areas, devices = [], [], {}, {}
    for row in rows[1:]:
        r = dict(zip(head, row))
        attrs = attributes(r.get("eigenschappen") or "")
        attrs["friendly_name"] = r["naam"]
        if r.get("device_class"):
            attrs["device_class"] = r["device_class"]
        if r.get("eenheid"):
            attrs["unit_of_measurement"] = r["eenheid"]
        states.append({"entity_id": r["entity_id"], "state": str(r["status"]), "attributes": attrs,
                       "last_changed": r.get("laatst gewijzigd") or None})
        entry = {"entity_id": r["entity_id"]}
        area = r.get("ruimte") or ""
        if area:
            areas.setdefault(slug(area), area)
        if r.get("apparaat"):
            device_id = slug(f"{r['apparaat']} {area}")
            devices[device_id] = {"id": device_id, "name": r["apparaat"], "area_id": slug(area) if area else None}
            entry["device_id"] = device_id
        elif area:
            entry["area_id"] = slug(area)
        if r.get("aliassen"):
            entry["aliases"] = [a.strip() for a in str(r["aliassen"]).split(",")]
        entities.append(entry)
    return {"states": states, "entities": entities, "devices": list(devices.values()),
            "areas": [{"area_id": k, "name": v} for k, v in areas.items()]}


if __name__ == "__main__":
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(convert(sys.argv[1]), f, ensure_ascii=False, indent=0)
