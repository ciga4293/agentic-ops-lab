#!/usr/bin/env python3
"""
ETL Deterministik Tahap 1 — Recap Surat Jalan & Administrasi DO (TWB On Call).

Fitur utama:
1. Membaca export mentah Odoo (.csv / .xlsx) tanpa perlu pre-cleaning Power Query.
2. Forward-fill kolom header per blok order (`Order Reference`, `No. BL/DO`,
   `Company`, `Customer`, `Shipper`, `No Polisi`).
3. Filter armada aktif dari `fleet_twb_oncall.json` (normalisasi huruf & angka).
4. Penanganan multi-product dalam 1 SO:
   - Baris utama -> `rute_clean` (`ALL CAPS`, `ASAL - TUJUAN`, tanpa prefix customer).
   - Baris biaya tambahan (`TKBM`, `MEL`, `INAP`, `KAWALAN`, dll.) -> `has_tkbm`
     & `extra_charges` / `notes_suggestion` tanpa menimpa rute.
5. Pairing `SO/RST/...` dan `SO/IBL/...` via `Order Lines/JO Importir`.
6. Normalisasi nama driver via `drivers.json` & aturan vendor (`needs_do_from_sj`).
7. Deduplikasi & incremental state via `processed_jobs_state.json`, menghasilkan
   `odoo_sj_staging_latest.json` dan `odoo_sj_staging_latest.csv`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FLEET_CONFIG = BASE_DIR / "config" / "fleet_twb_oncall.json"
DEFAULT_VENDOR_RULES = BASE_DIR / "config" / "vendor_rules.json"
DEFAULT_DRIVERS_CONFIG = BASE_DIR / "config" / "drivers.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "staging"
DEFAULT_STATE_FILE = DEFAULT_OUTPUT_DIR / "processed_jobs_state.json"

DEFAULT_DIVISION_SUFFIXES = ("RST", "TWB", "IBL", "TCT", "KSO")

CSV_SUMMARY_FIELDS = [
    "job_key",
    "sync_status",
    "pairing_status",
    "so_rst",
    "so_ibl",
    "jo_importir",
    "nopol",
    "tgl_jalan",
    "rute_clean",
    "driver_canonical",
    "drivers_raw_text",
    "company",
    "customer",
    "shipper",
    "no_bl_do",
    "sj_done",
    "short_name_pdf",
    "status_resi",
    "needs_do_from_sj",
    "has_tkbm",
    "extra_charges_text",
    "notes_suggestion",
    "needs_review",
]


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


def load_state_file(state_path: Path) -> dict[str, Any]:
    """Muat state deduplikasi incremental; kembalikan struktur kosong jika belum ada."""
    if not state_path.exists():
        return {"last_updated": None, "jobs": {}}
    data = load_json_file(state_path, "processed jobs state")
    if "jobs" not in data or not isinstance(data["jobs"], dict):
        data["jobs"] = {}
    return data


def normalize_nopol(nopol: Any) -> str:
    """Normalisasi nomor polisi: hanya huruf & angka, uppercase, tanpa spasi."""
    if nopol is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(nopol).upper())


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


def build_driver_lookup(drivers_config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Bangun indeks lookup case-insensitive dari canonical_name dan aliases."""
    lookup: dict[str, dict[str, str]] = {}
    for entry in drivers_config.get("drivers", []):
        canonical = re.sub(r"\s+", " ", str(entry.get("canonical_name", "")).strip()).upper()
        if not canonical:
            continue
        status = str(entry.get("status", "active")).strip().lower()
        info = {"canonical_name": canonical, "status": status}
        lookup[canonical] = info
        for alias in entry.get("aliases", []):
            norm_alias = re.sub(r"\s+", " ", str(alias).strip()).upper()
            if norm_alias:
                lookup[norm_alias] = info
    return lookup


def strip_division_suffix(name: str, suffixes: tuple[str, ...]) -> str:
    """Hapus embel-embel suffix divisi di akhir nama (misal 'RST', 'TWB', '2 TWB')."""
    if not suffixes:
        return name
    suffix_group = "|".join(re.escape(s.upper()) for s in suffixes)
    pattern = rf"(?:\s+\d+)?\s+(?:{suffix_group})$"
    cleaned = re.sub(pattern, "", name.upper()).strip()
    return re.sub(r"\s+", " ", cleaned)


def normalize_driver_name(
    raw_name: Any,
    drivers_config: dict[str, Any],
    driver_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Cocokkan nama driver mentah dari Odoo/WA ke canonical_name (case-insensitive)."""
    raw_clean = re.sub(r"\s+", " ", str(raw_name or "").strip()).upper()
    if not raw_clean:
        return {
            "driver_raw": "",
            "driver_canonical": "",
            "driver_status": "unknown",
            "driver_matched": False,
        }

    lookup = driver_lookup if driver_lookup is not None else build_driver_lookup(drivers_config)
    if raw_clean in lookup:
        hit = lookup[raw_clean]
        return {
            "driver_raw": raw_clean,
            "driver_canonical": hit["canonical_name"],
            "driver_status": hit["status"],
            "driver_matched": True,
        }

    suffixes = tuple(drivers_config.get("division_suffixes") or DEFAULT_DIVISION_SUFFIXES)
    stripped = strip_division_suffix(raw_clean, suffixes)
    if stripped in lookup:
        hit = lookup[stripped]
        return {
            "driver_raw": raw_clean,
            "driver_canonical": hit["canonical_name"],
            "driver_status": hit["status"],
            "driver_matched": True,
        }

    return {
        "driver_raw": raw_clean,
        "driver_canonical": stripped,
        "driver_status": "unregistered",
        "driver_matched": False,
    }


def resolve_vendor_rule(
    customer: str, shipper: str, vendor_rules: dict[str, Any]
) -> dict[str, Any]:
    """Cocokkan Customer/Shipper ke aturan vendor untuk PDF, status resi, & cek SJ/DO."""
    haystack = f"{customer} {shipper}".upper()
    for rule in vendor_rules.get("vendors", []):
        keywords = rule.get("match_keywords", [])
        if any(str(kw).upper() in haystack for kw in keywords if kw):
            return {
                "vendor_name": str(rule.get("name", "")),
                "short_name_pdf": str(rule.get("short_name_pdf", "UMUM")),
                "default_status_resi": str(rule.get("default_status_resi", "Belum")),
                "requires_sj_do_check": bool(rule.get("requires_sj_do_check", False)),
            }

    default_rule = vendor_rules.get("default_rule", {})
    return {
        "vendor_name": "DEFAULT",
        "short_name_pdf": str(default_rule.get("short_name_pdf", "UMUM")),
        "default_status_resi": str(default_rule.get("default_status_resi", "Belum")),
        "requires_sj_do_check": bool(default_rule.get("requires_sj_do_check", False)),
    }


def is_extra_cost_item(text: str, extra_cost_keywords: list[str]) -> bool:
    """Cek apakah item baris order termasuk komponen biaya tambahan."""
    upper_text = text.upper()
    return any(
        re.search(rf"\b{re.escape(kw.upper())}\b", upper_text)
        for kw in extra_cost_keywords
        if kw
    )


def clean_route_text(raw_product: str) -> str:
    """Ambil segmen rute terakhir berpola 'ASAL - TUJUAN' (ALL CAPS) tanpa prefix."""
    text = str(raw_product or "").strip()
    if not text:
        return ""

    segments = [seg.strip() for seg in text.split("/") if seg.strip()]
    route_seg = ""
    for seg in reversed(segments):
        if re.search(r"[A-Za-z0-9]\s*-\s*[A-Za-z0-9]", seg):
            route_seg = seg
            break

    if not route_seg:
        return ""

    route_seg = re.sub(r"\s*-\s*", " - ", route_seg)
    return re.sub(r"\s+", " ", route_seg).strip().upper()


def get_field(row: dict[str, Any], *candidates: str) -> str:
    """Ambil nilai kolom pertama yang tersedia dan tidak kosong dari beberapa alias."""
    for key in candidates:
        if key in row and row[key] is not None and str(row[key]).strip() != "":
            return str(row[key]).strip()
    return ""


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


def forward_fill_order_headers(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forward-fill kolom header per blok order agar baris order lines mewarisi konteks."""
    filled_rows: list[dict[str, Any]] = []
    current_header: dict[str, str] = {
        "order_reference": "",
        "no_bl_do": "",
        "company": "",
        "customer": "",
        "shipper": "",
        "no_polisi": "",
    }

    for row in raw_rows:
        order_ref = get_field(row, "Order Reference")
        no_bl_do = get_field(row, "No. BL/DO", "NomorDO")
        company = get_field(row, "Company")
        customer = get_field(row, "Customer")
        shipper = get_field(row, "Shipper")
        no_polisi = get_field(row, "No Polisi", "Nopol")

        if order_ref:
            # Blok order baru dimulai: reset header dari baris ini.
            current_header = {
                "order_reference": order_ref,
                "no_bl_do": no_bl_do,
                "company": company,
                "customer": customer,
                "shipper": shipper,
                "no_polisi": no_polisi,
            }
        else:
            # Baris lanjutan order lines: warisi header yang kosong dari blok aktif.
            no_bl_do = no_bl_do or current_header["no_bl_do"]
            company = company or current_header["company"]
            customer = customer or current_header["customer"]
            shipper = shipper or current_header["shipper"]
            no_polisi = no_polisi or current_header["no_polisi"]
            order_ref = current_header["order_reference"]

        filled_rows.append(
            {
                "order_reference": order_ref,
                "no_bl_do": no_bl_do,
                "jo_importir": get_field(row, "Order Lines/JO Importir", "JO Importir"),
                "company": company,
                "customer": customer,
                "shipper": shipper,
                "tgl_jalan": get_field(row, "Order Lines/Tgl Jalan", "Tgl Jalan", "TanggalDO"),
                "no_polisi": no_polisi,
                "nopol_clean": normalize_nopol(no_polisi),
                "driver_raw": get_field(row, "Order Lines/Driver", "Driver"),
                "product": get_field(
                    row, "Order Lines/Product", "Order Lines / Product", "Product"
                ),
                "sj_done": get_field(row, "SJ Done?", "Status"),
            }
        )

    return filled_rows


def extract_so_details(
    so_rows: list[dict[str, Any]], extra_cost_keywords: list[str]
) -> dict[str, Any]:
    """Ekstrak rute murni (baris utama) dan biaya tambahan dari seluruh baris dalam 1 SO."""
    rute_clean = ""
    rute_attempted = False
    has_tkbm = False
    extra_charges: list[str] = []

    for r in so_rows:
        product = r["product"]
        if not product:
            continue

        if is_extra_cost_item(product, extra_cost_keywords):
            if re.search(r"\bTKBM\b", product.upper()):
                has_tkbm = True
            if product not in extra_charges:
                extra_charges.append(product)
        elif not rute_attempted:
            rute_attempted = True
            rute_clean = clean_route_text(product)

    first = so_rows[0]
    jo_importir = next((r["jo_importir"] for r in so_rows if r["jo_importir"]), "")
    tgl_jalan = next((r["tgl_jalan"] for r in so_rows if r["tgl_jalan"]), "")
    driver_raw = next((r["driver_raw"] for r in so_rows if r["driver_raw"]), "")
    sj_done = next((r["sj_done"] for r in so_rows if r["sj_done"]), "")

    return {
        "order_reference": first["order_reference"],
        "jo_importir": jo_importir,
        "no_bl_do": first["no_bl_do"],
        "company": first["company"],
        "customer": first["customer"],
        "shipper": first["shipper"],
        "tgl_jalan": tgl_jalan,
        "nopol_clean": first["nopol_clean"],
        "driver_raw": driver_raw,
        "sj_done": sj_done,
        "rute_clean": rute_clean,
        "has_tkbm": has_tkbm,
        "extra_charges": extra_charges,
    }


def make_job_key(
    so_rst: list[str], so_ibl: list[str], nopol: str, tgl_jalan: str, rute_clean: str
) -> str:
    """Bangun unique key kanonik per job: SO/RST + SO/IBL atau fallback nopol|tgl|rute."""
    so_parts = sorted(set(so_rst)) + sorted(set(so_ibl))
    if so_parts:
        return "|".join(so_parts)
    return f"{nopol}|{tgl_jalan}|{rute_clean}"


def join_unique_values(values: list[str], sep: str = " | ") -> str:
    """Gabungkan nilai unik non-kosong sambil mempertahankan urutan kemunculan."""
    seen: set[str] = set()
    result: list[str] = []
    for val in values:
        cleaned = re.sub(r"\s+", " ", str(val or "").strip())
        if not cleaned:
            continue
        key = cleaned.upper()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return sep.join(result)


def select_priority_driver(
    raw_drivers: list[str],
    drivers_config: dict[str, Any],
    driver_lookup: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    """Prioritaskan driver dengan penanda 'TWB', lalu normalisasi ke canonical_name."""
    unique_raw: list[str] = []
    seen: set[str] = set()
    for d in raw_drivers:
        cleaned = re.sub(r"\s+", " ", str(d or "").strip()).upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique_raw.append(cleaned)

    if not unique_raw:
        return normalize_driver_name("", drivers_config, driver_lookup), []

    twb_candidates = [d for d in unique_raw if re.search(r"\bTWB\b", d)]
    chosen_raw = twb_candidates[0] if twb_candidates else unique_raw[0]
    return normalize_driver_name(chosen_raw, drivers_config, driver_lookup), unique_raw


def compute_job_fingerprint(record: dict[str, Any]) -> str:
    """Hitung hash deterministik atas field penting job untuk deteksi perubahan."""
    payload = {
        "so_rst": record["so_rst"],
        "so_ibl": record["so_ibl"],
        "nopol": record["nopol"],
        "tgl_jalan": record["tgl_jalan"],
        "rute_clean": record["rute_clean"],
        "driver_canonical": record["driver_canonical"],
        "drivers_raw": record["drivers_raw"],
        "company": record["company"],
        "customer": record["customer"],
        "shipper": record["shipper"],
        "no_bl_do": record["no_bl_do"],
        "sj_done": record["sj_done"],
        "needs_do_from_sj": record["needs_do_from_sj"],
        "has_tkbm": record["has_tkbm"],
        "extra_charges": record["extra_charges"],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def group_paired_sos(so_records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Gabungkan SO/RST dan SO/IBL yang saling merujuk pada nopol & tgl_jalan yang sama."""
    n = len(so_records)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def same_trip_context(a: dict[str, Any], b: dict[str, Any]) -> bool:
        if a["nopol_clean"] != b["nopol_clean"]:
            return False
        if a["tgl_jalan"] and b["tgl_jalan"] and a["tgl_jalan"] != b["tgl_jalan"]:
            return False
        return True

    ref_index: dict[str, list[int]] = defaultdict(list)
    jo_index: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    for idx, rec in enumerate(so_records):
        ref_upper = rec["order_reference"].upper()
        if ref_upper:
            ref_index[ref_upper].append(idx)
        jo_upper = rec["jo_importir"].strip().upper()
        if jo_upper:
            jo_index[(jo_upper, rec["nopol_clean"], rec["tgl_jalan"])].append(idx)

    # 1. Link SO yang JO Importir-nya merujuk ke Order Reference lain (mutual / one-way).
    for idx, rec in enumerate(so_records):
        jo_upper = rec["jo_importir"].strip().upper()
        if jo_upper in ref_index:
            for partner_idx in ref_index[jo_upper]:
                if partner_idx != idx and same_trip_context(rec, so_records[partner_idx]):
                    union(idx, partner_idx)

    # 2. Link SO yang berbagi JO Importir non-SO yang sama pada nopol & tgl_jalan sama.
    for indices in jo_index.values():
        if len(indices) > 1:
            base = indices[0]
            for other in indices[1:]:
                union(base, other)

    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for idx, rec in enumerate(so_records):
        components[find(idx)].append(rec)
    return list(components.values())


def process_odoo_export(
    raw_rows: list[dict[str, Any]],
    active_fleet: dict[str, dict[str, Any]],
    vendor_rules: dict[str, Any],
    drivers_config: dict[str, Any],
    prior_state: dict[str, Any],
) -> dict[str, Any]:
    """Jalankan pipeline lengkap: forward-fill, filter armada, pairing SO, dan deduplikasi."""
    filled_rows = forward_fill_order_headers(raw_rows)
    extra_keywords = [str(k) for k in vendor_rules.get("extra_cost_keywords", [])]
    driver_lookup = build_driver_lookup(drivers_config)

    # Filter armada aktif terlebih dahulu, lalu kelompokkan per Order Reference.
    so_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_inactive_rows = 0
    for row in filled_rows:
        if row["nopol_clean"] not in active_fleet:
            skipped_inactive_rows += 1
            continue
        so_key = row["order_reference"] or f"__NO_SO__|{row['nopol_clean']}|{row['tgl_jalan']}"
        so_groups[so_key].append(row)

    so_records = [
        extract_so_details(rows, extra_keywords) for rows in so_groups.values()
    ]
    paired_groups = group_paired_sos(so_records)

    prior_jobs: dict[str, Any] = prior_state.get("jobs", {})
    next_state_jobs: dict[str, Any] = dict(prior_jobs)

    all_jobs: list[dict[str, Any]] = []
    new_or_updated_jobs: list[dict[str, Any]] = []
    unchanged_count = 0

    for group_sos in paired_groups:
        # Urutkan agar SO/RST diproses sebelum SO/IBL untuk konsistensi metadata.
        ordered_sos = sorted(
            group_sos,
            key=lambda s: (
                0 if s["order_reference"].upper().startswith("SO/RST") else 1,
                s["order_reference"],
            ),
        )

        so_rst_list: list[str] = []
        so_ibl_list: list[str] = []
        extra_charges: list[str] = []
        raw_drivers: list[str] = []
        companies: list[str] = []
        customers: list[str] = []
        shippers: list[str] = []
        has_tkbm = False

        rute_clean = ""
        jo_importir = ""
        nopol_clean = ""
        tgl_jalan = ""
        no_bl_do = ""
        sj_done = ""

        for so in ordered_sos:
            ref = so["order_reference"]
            ref_upper = ref.upper()
            if ref_upper.startswith("SO/RST") and ref not in so_rst_list:
                so_rst_list.append(ref)
            elif ref_upper.startswith("SO/IBL") and ref not in so_ibl_list:
                so_ibl_list.append(ref)

            rute_clean = rute_clean or so["rute_clean"]
            jo_importir = jo_importir or so["jo_importir"]
            nopol_clean = nopol_clean or so["nopol_clean"]
            tgl_jalan = tgl_jalan or so["tgl_jalan"]
            no_bl_do = no_bl_do or so["no_bl_do"]
            sj_done = sj_done or so["sj_done"]
            has_tkbm = has_tkbm or so["has_tkbm"]

            if so["driver_raw"]:
                raw_drivers.append(so["driver_raw"])
            if so["company"]:
                companies.append(so["company"])
            if so["customer"]:
                customers.append(so["customer"])
            if so["shipper"]:
                shippers.append(so["shipper"])
            for ch in so["extra_charges"]:
                if ch not in extra_charges:
                    extra_charges.append(ch)

        company_merged = join_unique_values(companies)
        customer_merged = join_unique_values(customers)
        shipper_merged = join_unique_values(shippers)

        vendor_info = resolve_vendor_rule(customer_merged, shipper_merged, vendor_rules)
        driver_info, drivers_raw_unique = select_priority_driver(
            raw_drivers, drivers_config, driver_lookup
        )

        pairing_complete = bool(so_rst_list and so_ibl_list)
        needs_review = (
            (not rute_clean)
            or (not driver_info["driver_matched"])
            or (not pairing_complete)
        )

        notes_parts: list[str] = []
        if has_tkbm:
            notes_parts.append("TKBM")
        if extra_charges:
            notes_parts.append("Biaya: " + "; ".join(extra_charges))
        if not rute_clean:
            notes_parts.append("PERLU CEK RUTE")
        if not pairing_complete:
            notes_parts.append("PAIRING SO BELUM LENGKAP")
        if not driver_info["driver_matched"] and driver_info["driver_raw"]:
            notes_parts.append(f"DRIVER BELUM TERDAFTAR ({driver_info['driver_raw']})")

        so_rst_str = ", ".join(sorted(so_rst_list))
        so_ibl_str = ", ".join(sorted(so_ibl_list))
        job_key = make_job_key(so_rst_list, so_ibl_list, nopol_clean, tgl_jalan, rute_clean)

        job_record: dict[str, Any] = {
            "job_key": job_key,
            "pairing_status": "paired" if pairing_complete else "unpaired",
            "so_rst": so_rst_str,
            "so_ibl": so_ibl_str,
            "jo_importir": jo_importir,
            "nopol": nopol_clean,
            "nopol_display": active_fleet[nopol_clean].get("nopol_display", nopol_clean),
            "tgl_jalan": tgl_jalan,
            "rute_clean": rute_clean,
            "driver_canonical": driver_info["driver_canonical"],
            "driver_raw": driver_info["driver_raw"],
            "drivers_raw": drivers_raw_unique,
            "driver_matched": driver_info["driver_matched"],
            "company": company_merged,
            "customer": customer_merged,
            "shipper": shipper_merged,
            "no_bl_do": no_bl_do,
            "sj_done": sj_done,
            "short_name_pdf": vendor_info["short_name_pdf"],
            "status_resi": vendor_info["default_status_resi"],
            "needs_do_from_sj": vendor_info["requires_sj_do_check"],
            "has_tkbm": has_tkbm,
            "extra_charges": extra_charges,
            "notes_suggestion": " | ".join(notes_parts),
            "needs_review": needs_review,
        }

        fingerprint = compute_job_fingerprint(job_record)
        prev_entry = prior_jobs.get(job_key)
        if prev_entry and prev_entry.get("fingerprint") == fingerprint:
            sync_status = "unchanged"
            unchanged_count += 1
        else:
            sync_status = "new_or_updated"
            next_state_jobs[job_key] = {
                "fingerprint": fingerprint,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "so_rst": so_rst_str,
                "so_ibl": so_ibl_str,
                "no_bl_do": no_bl_do,
                "sj_done": sj_done,
            }

        job_record["sync_status"] = sync_status
        all_jobs.append(job_record)
        if sync_status == "new_or_updated":
            new_or_updated_jobs.append(job_record)

    return {
        "summary": {
            "total_raw_rows": len(raw_rows),
            "skipped_non_active_fleet_rows": skipped_inactive_rows,
            "total_jobs_in_batch": len(all_jobs),
            "new_or_updated_count": len(new_or_updated_jobs),
            "unchanged_count": unchanged_count,
            "needs_review_count": sum(1 for j in all_jobs if j["needs_review"]),
        },
        "new_or_updated_jobs": new_or_updated_jobs,
        "all_jobs": all_jobs,
        "next_state": {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "jobs": next_state_jobs,
        },
    }


def write_staging_csv(csv_path: Path, jobs: list[dict[str, Any]]) -> None:
    """Tulis ringkasan job new_or_updated ke file CSV untuk review cepat."""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_SUMMARY_FIELDS)
        writer.writeheader()
        for job in jobs:
            row = {k: job.get(k, "") for k in CSV_SUMMARY_FIELDS}
            row["extra_charges_text"] = "; ".join(job.get("extra_charges", []))
            row["drivers_raw_text"] = "; ".join(job.get("drivers_raw", []))
            writer.writerow(row)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL Deterministik Tahap 1 untuk Recap Surat Jalan (TWB On Call)."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path ke file export Odoo mentah (.csv atau .xlsx).",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        type=Path,
        help=f"Direktori output staging (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        type=Path,
        help=f"Path file state deduplikasi (default: {DEFAULT_STATE_FILE}).",
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
    parser.add_argument(
        "--drivers-config",
        default=DEFAULT_DRIVERS_CONFIG,
        type=Path,
        help=f"Path ke drivers.json (default: {DEFAULT_DRIVERS_CONFIG}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Jalankan simulasi tanpa menulis file output staging maupun mengubah state.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        active_fleet = load_active_fleet(args.fleet_config)
        vendor_rules = load_json_file(args.vendor_rules, "vendor rules")
        drivers_config = load_json_file(args.drivers_config, "drivers config")
        prior_state = load_state_file(args.state_file)

        raw_rows = read_tabular_input(args.input)
        result = process_odoo_export(
            raw_rows, active_fleet, vendor_rules, drivers_config, prior_state
        )

        summary = result["summary"]
        if args.dry_run:
            print(
                f"[DRY-RUN] {summary['total_raw_rows']} baris mentah -> "
                f"{summary['total_jobs_in_batch']} job "
                f"({summary['new_or_updated_count']} new/updated, "
                f"{summary['unchanged_count']} unchanged, "
                f"{summary['needs_review_count']} needs_review). "
                "Tidak ada file yang ditulis."
            )
            return 0

        args.output_dir.mkdir(parents=True, exist_ok=True)
        latest_json = args.output_dir / "odoo_sj_staging_latest.json"
        latest_csv = args.output_dir / "odoo_sj_staging_latest.csv"

        output_payload = {
            "summary": summary,
            "new_or_updated_jobs": result["new_or_updated_jobs"],
            "all_jobs": result["all_jobs"],
        }
        with latest_json.open("w", encoding="utf-8") as fh:
            json.dump(output_payload, fh, indent=2, ensure_ascii=False)

        write_staging_csv(latest_csv, result["new_or_updated_jobs"])

        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        with args.state_file.open("w", encoding="utf-8") as fh:
            json.dump(result["next_state"], fh, indent=2, ensure_ascii=False)

        print(
            f"[OK] {summary['total_raw_rows']} baris -> {summary['total_jobs_in_batch']} job "
            f"({summary['new_or_updated_count']} new/updated, {summary['unchanged_count']} unchanged).\n"
            f"     JSON : {latest_json}\n"
            f"     CSV  : {latest_csv}\n"
            f"     State: {args.state_file}"
        )
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
