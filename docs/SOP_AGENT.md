# SOP Umum Agentic Ops (`agentic-ops-lab`)

Dokumen ini menetapkan aturan operasional standar bagi seluruh agent (**OpenClaw**, **Hermes Agent**, maupun skrip otomasi) di workspace `agentic-ops-lab`.

---

## 1. Isolasi Workspace & Konteks
- **`workflows/office-ops/`**: Khusus untuk eksperimen dan otomasi operasional kantor (misalnya *Recap Surat Jalan*, *data cleaning*, sinkronisasi staging, dan metrik dashboard).
- **`workflows/content-cron/`**: Khusus untuk eksperimen penjadwalan konten (*idle time cron job*).
- **Aturan Pemisahan**: Agent, skrip, dan konfigurasi antar kedua domain **dilarang saling berbagi konteks, kredensial, atau data staging**. Masing-masing dijalankan di container terpisah (`openclaw-sandbox` / `hermes-sandbox`) sesuai [`docker-compose.yml`](../docker-compose.yml).

---

## 2. Zero Production Write
- Agent **dilarang keras** menulis, mengubah, atau menghapus data pada sistem produksi secara langsung (Odoo produksi, Google Sheet utama, database produksi, maupun kanal WhatsApp resmi).
- Seluruh proses baca/tulis hanya boleh menyentuh:
  - Data dummy atau hasil masking di `data/staging/`.
  - Salinan/duplikat Google Sheet yang dikhususkan untuk staging/lab.
  - File output lokal (CSV/XLSX/JSON/PDF) di folder staging/output.

---

## 3. Masking Data Sensitif Sebelum ke LLM API
- Sebelum mengirimkan teks, tabel, atau log ke LLM API komersial (OpenAI, Anthropic, Gemini), skrip pra-proses wajib melakukan **masking/redaksi** terhadap data sensitif:
  - Nomor telepon / WhatsApp driver atau PIC.
  - Nilai nominal sensitif, kredensial, token API, dan informasi pribadi (*PII*).
- Jika data dapat diproses secara lokal via **Ollama** atau skrip Python tanpa LLM, prioritaskan jalur lokal/deterministik terlebih dahulu.

---

## 4. Batas Maksimal Retry & Eskalasi Error
- Setiap eksekusi task agent memiliki batas **maksimal `retry = 2`** pada langkah yang gagal (baik kegagalan parsing, validasi skema, maupun pemanggilan tool/API).
- Jika masih gagal setelah 2 kali retry:
  1. **Hentikan eksekusi** langkah tersebut (dilarang melakukan *infinite loop*).
  2. Catat detail error, input bermasalah, dan jejak langkah terakhir ke log staging.
  3. **Eskalasi ke operator manusia** untuk ditinjau sebelum proses dilanjutkan.

---

## 5. Prioritas Skrip Deterministik untuk Data Besar
- Pengolahan data terstruktur berukuran besar (filter tabel, normalisasi string/nopol, join/pairing ID, agregasi angka, pemisahan biaya) **wajib dikerjakan oleh skrip Python deterministik** (0 token LLM).
- Agent LLM (dan Vision) hanya digunakan pada bagian yang membutuhkan penalaran kontekstual atau ekstraksi tidak terstruktur (misalnya membaca foto Surat Jalan, merangkum anomali, atau menyusun draf pesan).

---

## 6. Human-in-the-Loop (HITL) Sebelum Aksi Keluar
- Setiap aksi yang berdampak ke pihak eksternal atau sistem di luar sandbox **wajib melalui review dan persetujuan manusia**, antara lain:
  - Upload data atau dokumen PDF ke **Odoo**.
  - Pengiriman pesan **WhatsApp** ke driver atau tim operasional.
  - Publikasi konten atau pembaruan ke sistem eksternal.
- Output dari agent untuk aksi keluar hanya berupa **draf, daftar antrean, atau file siap unggah** yang dieksekusi manual setelah diverifikasi operator.
