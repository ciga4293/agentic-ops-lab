#!/usr/bin/env python3
"""
ETL Deterministik Tahap 1 — Recap Surat Jalan & Administrasi DO (TWB On Call).

Membaca file export Odoo (.csv / .xlsx), memfilter armada aktif dari
`fleet_twb_oncall.json`, menerapkan aturan vendor dari `vendor_rules.json`,
memisahkan biaya tambahan dari rute murni, serta memasangkan SO/RST dan SO/IBL
berdasarkan `JO Importir`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FLEET_CONFIG = BASE_DIR / "config" / "fleet_twb_oncall.json"
DEFAULT_VENDOR_RULES = BASE_DIR / "config" / "vendor_rules.json"
DEFAULT_OUTPUT_PATH = BASE_DIR / "staging" / "etl_odoo_sj_output.json"


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    """Muat file JSON dengan pesan error yang jelas jika gagal."""
    if not path.exists():
        raise FileNotFoundError(f"File {label} tidak ditemukan: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Format JSON tidak valid pada {label} ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Struktur root {label} harus berupa objek JSON: {path}")
    return data


def normalize_nopol(nopol: Any) -> str:
    """Normalisasi nomor polisi: hapus semua spasi dan ubah ke huruf kapital."""
    if nopol is None:
        return ""
    return re.sub(r"\s+", "", str(nopol)).upper()


def load_active_fleet(fleet_path: Path) -> dict[str, dict[str, Any]]:
    """Ambil daftar armada berstatus 'active' terindeks oleh nopol ternormalisasi."""
    data = load_json_file(fleet_path, "fleet config")
    active_units: dict[str, dict[str, Any]] = {}
    for unit in data.get("active_units", []):
        if str(unit.get("status", "")).strip().lower() != "active":
            continue
        clean = normalize_nopol(unit.get("nopol_clean") or unit.get("nopol_display"))
        if clean:
            active_units[clean] = unit
    return active_units


def resolve_vendor_rule(
    customer: str, shipper: str, vendor_rules: dict[str, Any]
) -> dict[str, str]:
    """Cocokkan Customer/Shipper ke aturan vendor untuk short_name_pdf & status resi."""
    haystack = f"{customer} {shipper}".upper()
    for rule in vendor_rules.get("vendors", []):
        keywords = rule.get("match_keywords", [])
        if any(str(kw).upper() in haystack for kw in keywords if kw):
            return {
                "vendor_name": str(rule.get("name", "")),
                "short_name_pdf": str(rule.get("short_name_pdf", "UMUM")),
                "default_status_resi": str(rule.get("default_status_resi", "Belum")),
            }

    default_rule = vendor_rules.get("default_rule", {})
    return {
        "vendor_name": "DEFAULT",
        "short_name_pdf": str(default_rule.get("short_name_pdf", "UMUM")),
        "default_status_resi": str(default_rule.get("default_status_resi", "Belum")),
    }


def is_extra_cost_item(text: str, extra_cost_keywords: list[str]) -> bool:
    """Cek apakah item baris order termasuk komponen biaya tambahan."""
    upper_text = text.upper()
    return any(
        re.search(rf"\b{re.escape(kw.upper())}\b", upper_text)
        for kw in extra_cost_keywords
        if kw
    )


def normalize_route(raw_route: str) -> str:
    """Bersihkan rute menjadi ALL CAPS tanpa prefix sebelum '/' jika ada."""
    cleaned = str(raw_route or "").strip()
    if "/" in cleaned:
        cleaned = cleaned.split("/", 1)[1]
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
    return cleaned


def split_route_and_costs(
    items: list[dict[str, Any]], extra_cost_keywords: list[str]
) -> dict[str, Any]:
    """Pisahkan item rute murni dari item biaya tambahan (TKBM, MEL, INAP, dll.)."""
    pure_routes: list[str] = []
    cost_notes: list[str] = []
    extra_cost_total = 0.0
    route_subtotal = 0.0

    for item in items:
        product = str(item.get("product", "")).strip()
        subtotal = parse_number(item.get("subtotal", 0))
        if not product:
            continue

        if is_extra_cost_item(product, extra_cost_keywords):
            cost_notes.append(f"{product} ({subtotal:g})" if subtotal else product)
            extra_cost_total += subtotal
        else:
            normalized = normalize_route(product)
            if normalized and normalized not in pure_routes:
                pure_routes.append(normalized)
            route_subtotal += subtotal

    return {
        "rute": " | ".join(pure_routes),
        "tkbm_flag": len(cost_notes) > 0,
        "catatan_biaya": "; ".join(cost_notes),
        "subtotal_rute": route_subtotal,
        "subtotal_biaya_tambahan": extra_cost_total,
    }


def parse_number(value: Any) -> float:
    """Konversi nilai numerik dari CSV/XLSX secara aman."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def read_tabular_input(input_path: Path) -> list[dict[str, Any]]:
    """Baca file export Odoo berformat .csv atau .xlsx menjadi list of dict."""
    if not input_path.exists():
        raise FileNotFoundError(f"File input tidak ditemukan: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            return [dict(row) for row in reader]

    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:
            raise RuntimeError(
                "Membaca file .xlsx membutuhkan library 'openpyxl'. "
                "Pasang dengan: pip install openpyxl"
            ) from exc

        wb = openpyxl.load_workbook(input_path, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = next(rows_iter, None)
        if not headers:
            return []
        clean_headers = [str(h).strip() if h is not None else "" for h in headers]
        records: list[dict[str, Any]] = []
        for row in rows_iter:
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue
            records.append(dict(zip(clean_headers, row)))
        return records

    raise ValueError(
        f"Ekstensi file tidak didukung: '{suffix}'. Gunakan .csv atau .xlsx."
    )


def get_field(row: dict[str, Any], *candidates: str) -> str:
    """Ambil nilai kolom pertama yang tersedia dan tidak kosong dari beberapa alias."""
    for key in candidates:
        if key in row and row[key] is not None and str(row[key]).strip() != "":
            return str(row[key]).strip()
    return ""


def process_odoo_export(
    rows: list[dict[str, Any]],
    active_fleet: dict[str, dict[str, Any]],
    vendor_rules: dict[str, Any],
) -> dict[str, Any]:
    """Filter armada aktif, pasangkan SO/RST + SO/IBL via JO Importir, dan bersihkan data."""
    extra_keywords = [str(k) for k in vendor_rules.get("extra_cost_keywords", [])]
    grouped_by_jo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_inactive_fleet = 0
    unpaired_no_jo: list[dict[str, Any]] = []

    for row in rows:
        nopol_clean = normalize_nopol(get_field(row, "Nopol"))
        if nopol_clean not in active_fleet:
            skipped_inactive_fleet += 1
            continue

        jo_importir = get_field(row, "JO Importir")
        if not jo_importir:
            unpaired_no_jo.append(
                {
                    "order_reference": get_field(row, "Order Reference"),
                    "nopol": nopol_clean,
                    "reason": "JO Importir kosong",
                }
            )
            continue

        grouped_by_jo[jo_importir].append(row)

    paired_records: list[dict[str, Any]] = []
    for jo_importir, jo_rows in grouped_by_jo.items():
        so_rst_refs: list[str] = []
        so_ibl_refs: list[str] = []
        other_refs: list[str] = []
        line_items: list[dict[str, Any]] = []

        # Gunakan metadata dari baris pertama yang terisi pada grup JO Importir ini.
        customer = ""
        shipper = ""
        tanggal_do = ""
        nomor_do = ""
        nopol_clean = ""
        driver = ""
        status_do = ""

        for r in jo_rows:
            order_ref = get_field(r, "Order Reference")
            upper_ref = order_ref.upper()
            if upper_ref.startswith("SO/RST") and order_ref not in so_rst_refs:
                so_rst_refs.append(order_ref)
            elif upper_ref.startswith("SO/IBL") and order_ref not in so_ibl_refs:
                so_ibl_refs.append(order_ref)
            elif order_ref and order_ref not in other_refs:
                other_refs.append(order_ref)

            customer = customer or get_field(r, "Customer")
            shipper = shipper or get_field(r, "Shipper")
            tanggal_do = tanggal_do or get_field(r, "Tgl Jalan", "TanggalDO")
            nomor_do = nomor_do or get_field(r, "No. BL/DO", "NomorDO")
            nopol_clean = nopol_clean or normalize_nopol(get_field(r, "Nopol"))
            driver = driver or get_field(r, "Driver")
            status_do = status_do or get_field(r, "Status")

            line_items.append(
                {
                    "product": get_field(r, "Order Lines / Product", "Order Lines/Product", "Product"),
                    "subtotal": get_field(r, "Subtotal"),
                }
            )

        route_info = split_route_and_costs(line_items, extra_keywords)
        vendor_info = resolve_vendor_rule(customer, shipper, vendor_rules)

        pairing_complete = bool(so_rst_refs and so_ibl_refs)
        paired_records.append(
            {
                "jo_importir": jo_importir,
                "so_rst": so_rst_refs,
                "so_ibl": so_ibl_refs,
                "other_refs": other_refs,
                "pairing_status": "paired" if pairing_complete else "incomplete_pair",
                "customer": customer,
                "shipper": shipper,
                "tanggal_do": tanggal_do,
                "nomor_do": nomor_do,
                "nopol": nopol_clean,
                "nopol_display": active_fleet[nopol_clean].get("nopol_display", nopol_clean),
                "driver": driver,
                "status_do": status_do,
                "short_name_pdf": vendor_info["short_name_pdf"],
                "status_resi": vendor_info["default_status_resi"],
                **route_info,
            }
        )

    return {
        "summary": {
            "total_raw_rows": len(rows),
            "skipped_non_active_fleet": skipped_inactive_fleet,
            "unpaired_missing_jo": len(unpaired_no_jo),
            "total_jo_groups": len(paired_records),
            "incomplete_pairs": sum(
                1 for rec in paired_records if rec["pairing_status"] != "paired"
            ),
        },
        "records": paired_records,
        "anomalies_missing_jo": unpaired_no_jo,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL Deterministik Tahap 1 untuk Recap Surat Jalan (TWB On Call)."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path ke file export Odoo (.csv atau .xlsx).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        type=Path,
        help=f"Path output JSON staging (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--fleet-config",
        default=DEFAULT_FLEET_CONFIG,
        type=Path,
        help=f"Path ke fleet_twb_oncall.json (default: {DEFAULT_FLEET_CONFIG}).",
    )
    parser.add_argument(
        "--vendor-rules",
        default=DEFAULT_VENDOR_RULES,
        type=Path,
        help=f"Path ke vendor_rules.json (default: {DEFAULT_VENDOR_RULES}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        active_fleet = load_active_fleet(args.fleet_config)
        vendor_rules = load_json_file(args.vendor_rules, "vendor rules")
        raw_rows = read_tabular_input(args.input)
        result = process_odoo_export(raw_rows, active_fleet, vendor_rules)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)

        summary = result["summary"]
        print(
            f"[OK] Selesai memproses {summary['total_raw_rows']} baris -> "
            f"{summary['total_jo_groups']} grup JO Importir disimpan ke {args.output}"
        )
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
