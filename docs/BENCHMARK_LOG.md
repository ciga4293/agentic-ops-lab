# Benchmark Log — OpenClaw vs Hermes

Catatan eksperimen dan metrik portofolio untuk membandingkan performa, efisiensi biaya, dan penghematan waktu antara **OpenClaw** dan **Hermes Agent** menggunakan LLM Lokal (Ollama) maupun API.

## Tabel Metrik Eksperimen

| Tanggal | Nama Task | Kategori (Ringan/Menengah/Kompleks) | Agent (OpenClaw/Hermes) | Engine (Local/API + Nama Model) | Total Token | Estimasi Biaya (Rp) | Waktu Eksekusi (Detik) | Waktu Manual (Menit) | Status & Catatan Error |
| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| 2026-11-26 | Pembersihan CSV Data Staging Harian | Menengah | OpenClaw | Local (Ollama: qwen2.5-coder:7b) | 4.850 | 0 | 28 | 25 | Sukses — Output valid, 0 error |

---

## Ringkasan Kalkulasi ROI

> Perbarui ringkasan di bawah secara berkala setiap kali baris baru ditambahkan ke tabel metrik.

- **Total Task Berhasil:** 1
- **Total Waktu Manual yang Dihemat:** 25 menit (0,42 jam)
- **Total Waktu Eksekusi Agent:** 28 detik (0,01 jam)
- **Total Jam Dihemat Bersih (Net Hours Saved):** **0,41 jam**
- **Total Biaya API Terpakai:** **Rp 0**
- **Rasio Efisiensi (Jam Dihemat vs Biaya API):** Sangat efisien (0,41 jam dihemat dengan biaya Rp 0 pada uji coba model lokal)
