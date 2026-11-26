# SOP Tugas 1 — Recap Surat Jalan & Administrasi DO (TWB On Call)

**Fase:** Fase 1 (Manual / CLI-triggered + Human-in-the-Loop)  
**Divisi:** TWB On Call  
**Prinsip:** Mengacu pada [SOP Umum Agentic Ops](SOP_AGENT.md) (Zero Production Write, deterministik lebih dulu, HITL sebelum aksi keluar).

---

## 1. Tujuan
Mengotomatiskan proses rekapitulasi Delivery Order (DO) langsung dari file export mentah Odoo (tanpa perlu *pre-cleaning* manual di Power Query), pencocokan foto/catatan Surat Jalan (SJ), pembaruan Google Sheet duplikat staging, pembuatan status monitoring dashboard, dan penyusunan draf pengingat WhatsApp untuk driver.

---

## 2. Input yang Dibutuhkan
1. **Export Mentah Odoo (`.xlsx` / `.csv`)** di `workflows/office-ops/input/` dengan kolom standar Odoo:
   - `Order Reference` (`SO/RST/...` atau `SO/IBL/...`)
   - `No. BL/DO`
   - `Order Lines/JO Importir`
   - `Company`, `Customer`, `Shipper`
   - `Order Lines/Tgl Jalan`
   - `No Polisi`
   - `Order Lines/Driver`
   - `Order Lines/Product`
   - `SJ Done?`
2. **Snapshot Google Sheet Duplikat:** `Surat Jalan Checklist TWB 2026` (salinan staging, bukan sheet produksi).
3. **Foto & Catatan WA:** Berkas gambar Surat Jalan serta catatan teks dari grup/chat WA di folder input staging.
4. **Konfigurasi Referensi:**
   - Daftar armada aktif: [`workflows/office-ops/config/fleet_twb_oncall.json`](../workflows/office-ops/config/fleet_twb_oncall.json)
   - Daftar driver & alias: [`workflows/office-ops/config/drivers.json`](../workflows/office-ops/config/drivers.json)
   - Aturan khusus vendor & biaya: [`workflows/office-ops/config/vendor_rules.json`](../workflows/office-ops/config/vendor_rules.json)

---

## 3. Alur Eksekusi

### Tahap 1 — ETL Deterministik via Python (0 Token)
Dijalankan melalui [`etl_odoo_sj.py`](../workflows/office-ops/scripts/etl_odoo_sj.py) tanpa memanggil LLM:
1. **Forward-Fill Blok Order:**
   - Mengisi otomatis kolom header (`Order Reference`, `No. BL/DO`, `Company`, `Customer`, `Shipper`, `No Polisi`) pada baris-baris *order lines* di bawahnya yang kosong, dan me-reset blok saat `Order Reference` baru muncul.
2. **Filter Armada Aktif:**
   - Membaca [`fleet_twb_oncall.json`](../workflows/office-ops/config/fleet_twb_oncall.json) dan mengambil hanya unit dengan `"status": "active"`.
   - Normalisasi `No Polisi` menjadi huruf & angka uppercase tanpa spasi (misal `B 9118 VEU` → `B9118VEU`).
3. **Penanganan Multi-Product & Pemisahan Biaya Tambahan:**
   - Baris pertama/utama dipakai untuk mengekstrak `rute_clean`: buang prefix sebelum `/` jika berisi nama customer/vendor, ubah ke `ALL CAPS`, dan rapikan menjadi format `ASAL - TUJUAN`.
   - Jika rute kosong/terpotong (misal `LOGISLY/`), set `rute_clean = ""` dan `needs_review = true`.
   - Baris-baris berikutnya yang mengandung kata kunci biaya tambahan (`TKBM`, `MEL`, `INAP`, `KAWALAN`, `BONGKAR`, `MUAT`, `LOF`, `GATEPASS`, `BIAYA`) **tidak** menimpa rute; set `has_tkbm = true` (jika ada `TKBM`) dan kumpulkan ke `extra_charges` / `notes_suggestion`.
4. **Pairing `SO/RST` dan `SO/IBL`:**
   - Memasangkan `SO/RST/...` dan `SO/IBL/...` berdasarkan `Order Lines/JO Importir` (atau rujukan nomor SO) menjadi satu record job terpadu.
5. **Normalisasi Driver & Aturan Vendor:**
   - Mencocokkan `Order Lines/Driver` ke `driver_canonical` pada [`drivers.json`](../workflows/office-ops/config/drivers.json) secara *case-insensitive*, dengan fallback penghapusan suffix divisi (`RST`, `TWB`, `IBL`, `TCT`, `KSO`).
   - Menerapkan [`vendor_rules.json`](../workflows/office-ops/config/vendor_rules.json) untuk menentukan `short_name_pdf`, `status_resi`, serta flag `needs_do_from_sj = true` bagi vendor yang wajib cek nomor DO dari foto SJ (**`KLOG`** dan **`SPX` / `CGL // SPX`**).
6. **Deduplikasi & Incremental State Tracking:**
   - Setiap job diberi `job_key` unik (kombinasi terurut `so_rst` + `so_ibl`, atau fallback `nopol|tgl_jalan|rute_clean`) dan dicocokkan terhadap [`workflows/office-ops/staging/processed_jobs_state.json`](../workflows/office-ops/staging/processed_jobs_state.json).
   - Job yang identik dengan state sebelumnya ditandai `unchanged` (tidak dikirim ulang ke agent); job baru atau yang mengalami perubahan field penting (misal `no_bl_do` atau `sj_done`) ditandai `new_or_updated` dan ditulis ke:
     - `workflows/office-ops/staging/odoo_sj_staging_latest.json`
     - `workflows/office-ops/staging/odoo_sj_staging_latest.csv`

### Tahap 2 — Agent + Vision + Skrip (Human-in-the-Loop)
Hanya memproses job berstatus `new_or_updated`:
1. **Konversi Foto SJ menjadi PDF Terstandar:**
   - Gunakan modul Vision / skrip pengolah dokumen untuk membaca informasi kunci dari foto SJ (termasuk verifikasi nomor DO untuk job dengan `needs_do_from_sj = true`), lalu konversi menjadi PDF dengan format nama:
     ```text
     Surat Jalan [short_name_pdf] [DDMMYYYY] [Nopol].pdf
     ```
2. **Update Google Sheet Duplikat (`Surat Jalan Checklist TWB 2026`):**
   - Tulis baris baru atau perbarui kolom administrasi DO pada sheet duplikat.
   - **Aturan Wajib:** Jangan pernah menimpa kolom `Realisasi` yang sudah terisi secara manual.
3. **Output JSON Status Monitoring:**
   - Hasilkan ringkasan status DO, kelengkapan SJ/PDF, dan status resi untuk dashboard monitoring.
4. **Daftar Draf Pengingat WA Driver:**
   - Saring DO dengan `SJ Done?` / status `Selesai` tetapi dokumen SJ belum terkirim atau `StatusResi` masih `Belum`.
   - Susun daftar draf pesan pengingat WhatsApp per driver untuk direview operator.

---

## 4. Batasan & Persetujuan Manual (HITL)
- **Upload PDF/data ke Odoo** dan **pengiriman pesan WhatsApp ke driver** **tidak dilakukan otomatis** oleh agent pada Fase 1.
- Operator melakukan review terhadap output staging (`odoo_sj_staging_latest.csv` / `.json`), PDF hasil konversi, pembaruan di Google Sheet duplikat, dan draf WA sebelum mengeksekusi aksi keluar secara manual.
- Jika terjadi kegagalan parsing atau ketidakcocokan data setelah maksimal **2 kali retry**, proses dihentikan dan dieskalasi ke operator.
