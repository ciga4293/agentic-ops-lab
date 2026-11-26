# SOP Tugas 1 — Recap Surat Jalan & Administrasi DO (TWB On Call)

**Fase:** Fase 1 (Manual / CLI-triggered + Human-in-the-Loop)  
**Divisi:** TWB On Call  
**Prinsip:** Mengacu pada [SOP Umum Agentic Ops](SOP_AGENT.md) (Zero Production Write, deterministik lebih dulu, HITL sebelum aksi keluar).

---

## 1. Tujuan
Mengotomatiskan proses rekapitulasi Delivery Order (DO) dari export Odoo, pencocokan foto/catatan Surat Jalan (SJ), pembaruan Google Sheet duplikat staging, pembuatan status monitoring dashboard, dan penyusunan draf pengingat WhatsApp untuk driver secara terukur dan aman.

---

## 2. Input yang Dibutuhkan
1. **Export Odoo (`.xlsx` / `.csv`)** di folder input staging dengan kolom minimal:
   - `Order Reference` (`SO/RST/...` atau `SO/IBL/...`)
   - `JO Importir`
   - `Customer`
   - `Shipper`
   - `Tgl Jalan` (`TanggalDO`)
   - `No. BL/DO` (`NomorDO`)
   - `Nopol`
   - `Driver`
   - `Order Lines / Product`
   - `Subtotal`
   - `Status`
2. **Snapshot Google Sheet Duplikat:** `Surat Jalan Checklist TWB 2026` (salinan staging, bukan sheet produksi).
3. **Foto & Catatan WA:** Berkas gambar Surat Jalan serta catatan teks dari grup/chat WA di folder input staging.
4. **Konfigurasi Referensi:**
   - Daftar armada aktif: [`workflows/office-ops/config/fleet_twb_oncall.json`](../workflows/office-ops/config/fleet_twb_oncall.json)
   - Aturan khusus vendor: [`workflows/office-ops/config/vendor_rules.json`](../workflows/office-ops/config/vendor_rules.json)

---

## 3. Alur Eksekusi

### Tahap 1 — Pemrosesan Deterministik via Python (0 Token)
Seluruh logika tabular dijalankan menggunakan skrip Python tanpa memanggil LLM:
1. **Filter Armada Aktif:**
   - Baca [`fleet_twb_oncall.json`](../workflows/office-ops/config/fleet_twb_oncall.json) dan ambil hanya unit dengan `"status": "active"`.
   - Normalisasi kolom `Nopol` (hapus spasi dan ubah ke huruf kapital, misalnya `B 9118 VEU` -> `B9118VEU`) sebelum pencocokan.
2. **Pairing `SO` dan `SO IBL`:**
   - Kelompokkan dan pasangkan baris `SO/RST/...` (SO utama) dengan `SO/IBL/...` (`SO IBL`) berdasarkan kesamaan nilai `JO Importir`.
   - Tandai baris yang tidak memiliki pasangan lengkap untuk dicek operator.
3. **Pembersihan Rute & Pemisahan Biaya Tambahan:**
   - Identifikasi item `Order Lines / Product` yang mengandung kata kunci biaya tambahan: `TKBM`, `MEL`, `INAP`, `KAWALAN`, `BONGKAR`, `MUAT`.
   - Pisahkan item tersebut ke flag `TKBM` (boolean/penanda) dan kolom catatan biaya (beserta `Subtotal`-nya), sehingga kolom rute murni tetap bersih.
   - Format rute murni menjadi **`ALL CAPS`** dengan pola **`ASAL - TUJUAN`** tanpa prefix nama customer.
4. **Penerapan Aturan Vendor:**
   - Terapkan aturan dari [`vendor_rules.json`](../workflows/office-ops/config/vendor_rules.json) berdasarkan `Customer` / `Shipper` (contoh: vendor `CGL` atau `SPX` otomatis mendapat `StatusResi: Digital`, serta mapping `short_name_pdf` untuk penamaan file).

### Tahap 2 — Agent + Vision + Skrip (Human-in-the-Loop)
1. **Konversi Foto SJ menjadi PDF Terstandar:**
   - Gunakan modul Vision / skrip pengolah dokumen untuk membaca informasi kunci dari foto SJ (validasi tanggal, nopol, dan vendor/customer bila diperlukan), lalu konversi menjadi file PDF dengan format nama:
     ```text
     Surat Jalan [short_name_pdf] [DDMMYYYY] [Nopol].pdf
     ```
     *(Gunakan `Nopol` ternormalisasi tanpa spasi dan `short_name_pdf` dari `vendor_rules.json`.)*
2. **Update Google Sheet Duplikat (`Surat Jalan Checklist TWB 2026`):**
   - Tulis baris baru atau perbarui kolom administrasi DO pada sheet duplikat.
   - **Aturan Wajib:** Jangan pernah menimpa kolom `Realisasi` yang sudah terisi secara manual.
3. **Output JSON Status Monitoring:**
   - Hasilkan file JSON ringkasan status DO, kelengkapan SJ/PDF, dan status resi untuk dikonsumsi dashboard monitoring.
4. **Daftar Draf Pengingat WA Driver:**
   - Saring DO dengan `Status` = `Selesai` tetapi dokumen SJ belum terkirim atau `StatusResi` masih `Belum`.
   - Susun daftar draf pesan pengingat WhatsApp per driver (berisi Nopol, TanggalDO, NomorDO/Rute) untuk direview operator.

---

## 4. Batasan & Persetujuan Manual (HITL)
- **Upload PDF/data ke Odoo** dan **pengiriman pesan WhatsApp ke driver** **tidak dilakukan otomatis** oleh agent pada Fase 1.
- Operator melakukan review terhadap PDF hasil konversi, pembaruan di Google Sheet duplikat, JSON monitoring, dan daftar draf WA sebelum mengeksekusi aksi keluar secara manual.
- Jika terjadi kegagalan parsing atau ketidakcocokan data setelah maksimal **2 kali retry**, proses dihentikan dan dieskalasi ke operator.
