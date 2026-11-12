#!/usr/bin/env python3
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ========================= KONFIGURACJA =========================
# Każda instancja Optimizer ma własny global.xml i własny events.log.
# Dodaj tutaj wszystkie instancje znajdujące się na tym hoście.
OPTIMIZERS = [
#    {
#        "id": "optimizer25",
#        "name": "Optimizer 25",
#        "xml": "/var/opt/optimizer_java25/config/global.xml",
#        "log": "/var/opt/optimizer_java25/logs/events.log",
#    },
     {
         "id": "optimizer17",
         "name": "Optimizer 17",
         "xml": "/var/opt/optimizer_java17/configs/global.xml",
         "log": "/var/opt/optimizer_java17/logs/events.log",
     },
]

VERSION = 1.5

# ===================== DOMYŚLNE WARTOŚCI MONITORINGU =====================
# Używane przy generowaniu monitoring.json.example.
# Jednostki czasu Zabbix: s, m, h, d, w (nie „g” — godzina to 1h).
#
# interval / timeout / protocol NIE trafiają do JSON — delay itemów jest
# w template Zabbix (obecnie 1m dla ping/port).
DEFAULTS = {
    "sent_problem_after": "1h",
    "log_problem_after": "1h",       # received + device_query
    "ping_problem_after": "15m",
    "port_problem_after": "15m",
}

# Elementy techniczne XML. Nie są grupami logicznymi ani seriami.
TECHNICAL_CONTAINERS = {
    "modbus_sensors",
    "modbus_sensor_registers",
}

# Tagi / wzorce, które nie są value_id serii.
NON_SERIES_TAGS = TECHNICAL_CONTAINERS | {
    "mqtt_controller",
    "mqtt_controller_objects",
    "mqtt_controller_broker",
    "mqtt_controller_topic",
    "mqtt_controller_client",
    "mqtt_controller_active",
    "derived_expression",
    "derived_expression_objects",
    "derived_expression_active",
    "modbus_sensor_address",
    "modbus_sensor_port",
}

NON_SERIES_PREFIXES = (
    "mqtt_controller.",
    "derived_expression.",
    "modbus_sensor.",
)


def slug(value):
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value)).strip("_") or "item"


def child_text(el, name, default=""):
    child = el.find(name)
    if child is not None and child.text:
        return child.text.strip()
    return default


def is_db2mqtt(el):
    return el.attrib.get("db2mqtt", "").lower() == "true"


def looks_like_value_id(tag: str) -> bool:
    """Czy tag elementu wygląda na identyfikator serii (value_id)."""
    if not tag or tag.startswith("!"):
        return False
    if tag in NON_SERIES_TAGS:
        return False
    if any(tag.startswith(p) for p in NON_SERIES_PREFIXES):
        return False
    return "." in tag


def root_flag_true(root, tag: str, default=True) -> bool:
    el = root.find(tag)
    if el is None:
        el = root.find(f".//{tag}")
    if el is None or el.text is None or not str(el.text).strip():
        return default
    return str(el.text).strip().lower() in ("1", "true", "yes", "on")


def element_ip(el):
    # Aktualny Optimizer używa modbus_sensor_address.
    # Pozostałe nazwy są tylko bezpiecznym fallbackiem.
    for name in ("modbus_sensor_address", "ip", "address", "host"):
        value = child_text(el, name)
        if value:
            return value
    return ""


def build_parent_map(root):
    return {child: parent for parent in root.iter() for child in parent}


def has_db2mqtt_descendant(el):
    return any(
        is_db2mqtt(child)
        for child in el.iter()
        if child is not el
    )


def nearest_physical_ancestor(el, parent_map):
    current = parent_map.get(el)
    while current is not None:
        ip = element_ip(current)
        if ip:
            return current, ip
        current = parent_map.get(current)
    return None, ""


def nearest_logical_parent(el, parent_map, root):
    """
    Znajduje najbliższego rodzica logicznego na podstawie drzewa XML.

    Grupa logiczna:
      - nie posiada IP/adresu,
      - nie jest technicznym kontenerem,
      - zawiera gdzieś niżej serie db2mqtt=true.

    Nie używamy nazw ani prefixów value_id.
    """
    current = parent_map.get(el)

    while current is not None and current is not root:
        if (
            not element_ip(current)
            and current.tag not in TECHNICAL_CONTAINERS
            and has_db2mqtt_descendant(current)
        ):
            return current

        current = parent_map.get(current)

    return None


def make_series(value_id):
    return value_id


def make_group(group_id, name, group_type, address=""):
    d = DEFAULTS
    group = {
        "id": group_id,
        "name": name,
        "type": group_type,
        "log": {
            "received": {
                # device: zwykle wystarczy device_query; mqtt/derived: received ON
                "enabled": group_type != "device",
                # each = item/trigger per seria; group = jeden na całą grupę
                "mode": "each",
                "problem_after": d["log_problem_after"],
            },
            # device_query = log „Odpytanie fizycznego urządzenia… nie powiodło się”
            # (dawniej: poll_error — LLD nadal to akceptuje).
            "device_query": {
                "enabled": group_type == "device",
                "problem_after": d["log_problem_after"],
            },
        },
        "series": [],
    }

    if group_type == "device":
        group["address"] = address
        group["ping"] = {
            "enabled": True,
            "problem_after": d["ping_problem_after"],
        }
        group["ports"] = []

    return group


def add_series(group, value_id):
    if value_id not in group["series"]:
        group["series"].append(make_series(value_id))


def add_device_port(group, port):
    if not port:
        return

    # Aktualna struktura monitoring.json ma tablicę ports[].
    # Generator zna tylko port z global.xml, więc proponuje Modbus TCP.
    d = DEFAULTS
    port_entry = {
        "id": "modbus",
        "name": "Modbus TCP",
        "port": int(port) if str(port).isdigit() else port,
        "enabled": True,
        "problem_after": d["port_problem_after"],
    }

    if port_entry not in group["ports"]:
        group["ports"].append(port_entry)


def find_or_create_group(groups, key, group):
    if key not in groups:
        groups[key] = group
    return groups[key]


def extract_optimizer(opt):
    xml_path = Path(opt["xml"])

    if not xml_path.is_file():
        raise FileNotFoundError(
            f"[{opt['id']}] XML not found: {xml_path}"
        )

    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        raise ValueError(
            f"[{opt['id']}] cannot parse XML: {exc}"
        ) from exc

    parent_map = build_parent_map(root)

    groups = {}
    physical_nodes = set()

    # ------------------------------------------------------------
    # 1. Fizyczne urządzenia.
    #
    # Obecnie znamy je z modbus_sensors. Dodatkowo zapamiętujemy
    # węzły posiadające adres, aby relacja drzewa była jednoznaczna.
    # ------------------------------------------------------------
    for container in root.findall(".//modbus_sensors"):
        for dev in list(container):
            if not dev.tag.startswith("modbus_sensor."):
                continue

            address = element_ip(dev)
            if not address:
                continue

            physical_nodes.add(dev)

            num = dev.tag.split(".", 1)[1] if "." in dev.tag else dev.tag
            group_id = f"modbus_sensor_{slug(num)}"

            group = find_or_create_group(
                groups,
                group_id,
                make_group(
                    group_id,
                    f"Modbus Sensor {num}",
                    "device",
                    address,
                ),
            )

            port = child_text(dev, "modbus_sensor_port")
            add_device_port(group, port)

            regs = dev.find("modbus_sensor_registers")
            if regs is not None:
                for reg in list(regs):
                    value_id = reg.tag
                    if not value_id or value_id.startswith("!"):
                        continue
                    add_series(group, value_id)

    # ------------------------------------------------------------
    # 2. Serie db2mqtt=true poza wcześniej przetworzonymi
    #    rejestrami Modbus.
    #
    #    RELACJA JEST WYZNACZANA Z DRZEWA XML:
    #
    #      seria
    #        |
    #        +-- najbliższy rodzic z IP -> device
    #
    #      albo
    #
    #      seria
    #        |
    #        +-- najbliższy rodzic bez IP -> logical
    #
    #    Nie ma żadnych prefixów value_id.
    # ------------------------------------------------------------
    for el in root.iter():
        if not is_db2mqtt(el):
            continue

        value_id = el.tag
        if not value_id or value_id.startswith("!"):
            continue

        physical_parent, address = nearest_physical_ancestor(
            el, parent_map
        )

        if physical_parent is not None:
            num = (
                physical_parent.tag.split(".", 1)[1]
                if "." in physical_parent.tag
                else physical_parent.tag
            )
            group_id = f"modbus_sensor_{slug(num)}"

            group = find_or_create_group(
                groups,
                group_id,
                make_group(
                    group_id,
                    f"Modbus Sensor {num}",
                    "device",
                    address,
                ),
            )

            port = child_text(
                physical_parent,
                "modbus_sensor_port",
            )
            add_device_port(group, port)
            add_series(group, value_id)
            continue

        logical_parent = nearest_logical_parent(
            el, parent_map, root
        )

        if logical_parent is not None:
            group_id = f"logical_{slug(logical_parent.tag)}"

            group = find_or_create_group(
                groups,
                group_id,
                make_group(
                    group_id,
                    logical_parent.tag,
                    "logical",
                ),
            )

            add_series(group, value_id)
            continue

        # Seria bez fizycznego ani logicznego rodzica.
        # Nie tworzymy sztucznej grupy "other".
        group_id = "logical_root"

        group = find_or_create_group(
            groups,
            group_id,
            make_group(
                group_id,
                "Serie bez grupy",
                "logical",
            ),
        )

        add_series(group, value_id)

    # ------------------------------------------------------------
    # 3. mqtt_controller_objects — serie bez db2mqtt (tag = value_id).
    # ------------------------------------------------------------
    if root_flag_true(root, "mqtt_controller_active", default=True):
        for ctrl in root.findall(".//mqtt_controller"):
            for inst in list(ctrl):
                if not str(inst.tag).startswith("mqtt_controller."):
                    continue
                num = inst.tag.split(".", 1)[1] if "." in inst.tag else inst.tag
                objs = inst.find("mqtt_controller_objects")
                if objs is None:
                    continue
                client = child_text(inst, "mqtt_controller_client") or f"mqtt_controller.{num}"
                group_id = f"logical_mqtt_controller_{slug(num)}"
                group = find_or_create_group(
                    groups,
                    group_id,
                    make_group(
                        group_id,
                        client,
                        "logical",
                    ),
                )
                for child in list(objs):
                    if looks_like_value_id(child.tag):
                        add_series(group, child.tag)

    # ------------------------------------------------------------
    # 4. derived_expression_objects — seria = nazwa elementu (tag),
    #    nie atrybuty element1/element2/...
    # ------------------------------------------------------------
    if root_flag_true(root, "derived_expression_active", default=True):
        for block in root.findall(".//derived_expression"):
            for inst in list(block):
                if not str(inst.tag).startswith("derived_expression."):
                    continue
                num = inst.tag.split(".", 1)[1] if "." in inst.tag else inst.tag
                objs = inst.find("derived_expression_objects")
                if objs is None:
                    continue
                group_id = f"logical_derived_expression_{slug(num)}"
                group = find_or_create_group(
                    groups,
                    group_id,
                    make_group(
                        group_id,
                        f"derived_expression.{num}",
                        "logical",
                    ),
                )
                for child in list(objs):
                    # Pierwsze „wyrażenie” = tag elementu przed atrybutami element*.
                    if looks_like_value_id(child.tag):
                        add_series(group, child.tag)

    # Stabilna kolejność.
    result = list(groups.values())
    result.sort(key=lambda x: (x["type"], x["id"]))

    for group in result:
        group["series"].sort()
        # Gdy są serie pomiarowe — ping/port domyślnie wyłączone (log received wystarczy).
        if group.get("type") == "device" and group.get("series"):
            if isinstance(group.get("ping"), dict):
                group["ping"]["enabled"] = False
            for port in group.get("ports") or []:
                if isinstance(port, dict):
                    port["enabled"] = False

    return {
        "id": opt["id"],
        "name": opt["name"],
        "log": opt["log"],
        "log_monitoring": {
            "sent": {
                "enabled": True,
                "problem_after": DEFAULTS["sent_problem_after"],
            },
        },
        "groups": result,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate proposal monitoring.json from Optimizer "
            "global.xml files. Logical groups are inferred from "
            "the XML tree; value_id prefixes are not used."
        )
    )
    script_dir = Path(__file__).resolve().parent
    default_output = script_dir / "monitoring.json.example"

    parser.add_argument(
        "-o",
        "--output",
        default=str(default_output),
        help=(
            "Output proposal file (default: monitoring.json.example "
            "in the script directory)"
        ),
    )

    args = parser.parse_args()

    if not OPTIMIZERS:
        print(
            "ERROR: OPTIMIZERS is empty. Edit the configuration "
            "at the beginning of the script.",
            file=sys.stderr,
        )
        return 2

    try:
        optimizers = []

        for opt in OPTIMIZERS:
            optimizers.append(extract_optimizer(opt))

        output = {
            "version": VERSION,
            "description": (
                "PROPOZYCJA - przejrzyj ręcznie przed zapisaniem jako "
                "/etc/zabbix/monitoring.json. Grupy logiczne są "
                "ustalane na podstawie struktury drzewa global.xml, "
                "bez prefixów value_id."
            ),
            "optimizers": optimizers,
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        print(f"Generated proposal: {output_path}")

        for opt in optimizers:
            print(
                f"  {opt['id']}: "
                f"xml={next(x['xml'] for x in OPTIMIZERS if x['id'] == opt['id'])} "
                f"log={opt['log']}"
            )

            counts = {
                "device": 0,
                "logical": 0,
                "series": sum(len(g["series"]) for g in opt["groups"]),
            }

            for group in opt["groups"]:
                counts[group["type"]] = counts.get(group["type"], 0) + 1

            print(
                "    groups: "
                f"device={counts.get('device', 0)}, "
                f"logical={counts.get('logical', 0)}, "
                f"series={counts['series']}"
            )

        return 0

    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

