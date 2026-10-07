"""
Preset di mappatura Modbus per tipo di dispositivo.

Ogni preset è un file JSON in `user_devices/presets/<categoria>/<id>.json`,
generato da `tools/build_presets.py` a partire da progetti open source
(vedi il campo `source` di ogni file). Formato:

    {
      "id": "inverter/deye_hybrid",
      "category": "inverter",
      "manufacturer": "Deye",
      "model": "SG0*LP1 (hybrid monofase)",
      "source": {"project": ..., "url": ..., "license": ..., "file": ...},
      "blocks": [{"register_type": "holding", "start_address": "0x0003", "word_count": 120}],
      "variables": [{"name": "PV1 Power", "register_type": "holding", "address": "0x00BA",
                     "unit": "W", "conversion_factor": "1", "offset": 0,
                     "bit_length": 16, "is_signed": false, "endianness": "big"}],
      "computed": [{"name": "Power", "unit": "W", "formula": "Output_Power",
                    "show_on_graph": true, "show_in_homepage": true}]
    }

Il preset viene applicato una sola volta, quando si crea il device: crea i
blocchi di lettura e le variabili, che poi restano modificabili a mano.
"""

import json
from functools import lru_cache
from pathlib import Path

from django.db import transaction

PRESETS_DIR = Path(__file__).resolve().parent

# Categorie mostrate nel selettore, nell'ordine indicato
CATEGORIES = {
    "inverter": "Inverter",
}

EMPTY_CHOICE = ("", "— Nessuno (mappatura manuale) —")


@lru_cache(maxsize=None)
def load_presets() -> dict:
    """Ritorna {preset_id: preset_dict} per tutti i file JSON presenti."""
    presets = {}
    for category in CATEGORIES:
        for path in sorted((PRESETS_DIR / category).glob("*.json")):
            with open(path, encoding="utf-8") as f:
                preset = json.load(f)
            presets[preset["id"]] = preset
    return presets


def preset_label(preset: dict) -> str:
    return f"{preset['manufacturer']} — {preset['model']}"


def preset_choices() -> list:
    """Choices per il selettore, raggruppate per categoria (optgroup)."""
    presets = load_presets()
    choices = [EMPTY_CHOICE]
    for category, category_label in CATEGORIES.items():
        group = sorted(
            ((pid, preset_label(p)) for pid, p in presets.items() if p["category"] == category),
            key=lambda c: c[1].lower(),
        )
        if group:
            choices.append((category_label, group))
    return choices


@transaction.atomic
def apply_preset(device, preset_id: str) -> None:
    """Crea blocchi di lettura, variabili Modbus e variabili calcolate del preset."""
    from user_devices.models import ComputedVariable, ModbusMappingVariable, ModbusReadBlock

    preset = load_presets()[preset_id]

    for order, block in enumerate(preset["blocks"]):
        ModbusReadBlock.objects.create(
            device=device,
            register_type=block["register_type"],
            start_address=block["start_address"],
            word_count=block["word_count"],
            order=order,
        )

    ModbusMappingVariable.objects.bulk_create([
        ModbusMappingVariable(
            device=device,
            var_name=var["name"],
            register_type=var["register_type"],
            address=var["address"],
            unit=var.get("unit") or "",
            conversion_factor=var.get("conversion_factor", "1"),
            offset=var.get("offset", 0),
            bit_length=var.get("bit_length", 16),
            is_signed=var.get("is_signed", False),
            endianness=var.get("endianness", "big"),
            show_in_homepage=var.get("show_in_homepage", False),
            order=order,
        )
        for order, var in enumerate(preset["variables"])
    ])
    # show_on_graph passa da save() che ne garantisce uno solo per device
    for var in preset["variables"]:
        if var.get("show_on_graph"):
            mv =ModbusMappingVariable.objects.get(device=device, var_name=var["name"])
            mv.show_on_graph = True
            mv.save()

    # create() singolo: save() garantisce un solo show_on_graph per device
    for order, var in enumerate(preset.get("computed", [])):
        ComputedVariable.objects.create(
            device=device,
            var_name=var["name"],
            unit=var.get("unit") or "",
            formula=var["formula"],
            show_on_graph=var.get("show_on_graph", False),
            show_in_homepage=var.get("show_in_homepage", False),
            order=order,
        )

    device.preset = preset_id
    device.save(update_fields=["preset"])
