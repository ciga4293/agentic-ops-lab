# Agentic Ops Lab

Workspace eksperimen terisolasi untuk riset, benchmarking, dan otomasi berbasis agentic AI menggunakan **OpenClaw** dan **Hermes Agent**.

---

## Project Charter

### 1. Tujuan Proyek
- **Eksplorasi Arsitektur:** Membandingkan desain, kapabilitas tool-use, manajemen memori, serta orkestrasi task antara **OpenClaw** dan **Hermes Agent**.
- **Benchmarking LLM Lokal vs API:** Mengevaluasi performa, latensi, akurasi, dan efisiensi biaya antara model lokal (**Ollama**) dan model API komersial (OpenAI, Anthropic, Gemini).
- **Automasi Operasional & Monitoring:** Membangun workflow otomatis untuk pembersihan data (*data cleaning*), sinkronisasi database harian, hingga pelaporan ke dashboard monitoring.
- **Eksperimen Cron Job Konten:** Menguji penjadwalan otomatis pembuatan/kurasi konten di malam hari (*idle time*) secara terkontrol.

### 2. Ruang Lingkup & Fase
1. **Fase 1 — Sandbox & Setup:** Penyiapan struktur repositori terisolasi, environment container Docker terpisah, manajemen kredensial via `.env`, dan guardrails dasar.
2. **Fase 2 — Head-to-Head Local vs API:** Pengujian terukur antara OpenClaw dan Hermes pada task ringan, menengah, hingga kompleks menggunakan model Ollama lokal dan API, lalu dicatat di [docs/BENCHMARK_LOG.md](docs/BENCHMARK_LOG.md).
3. **Fase 3 — Automasi Data & Dashboard:** Implementasi skrip pada `workflows/office-ops/` untuk *data cleaning*, sinkronisasi database staging, dan integrasi metrik ke dashboard monitoring.
4. **Fase 4 — Cron Job Sampingan:** Implementasi dan observasi skrip terjadwal pada `workflows/content-cron/` untuk eksperimen konten saat sistem idle.

### 3. Guardrails & Keamanan
- **Wajib Data Staging / Masking:** Seluruh eksperimen wajib menggunakan data dummy atau data yang sudah di-masking di `data/staging/`. Dilarang keras memakai data produksi atau kredensial asli di dalam repositori.
- **Pemisahan Container:** Task operasional kantor dan eksperimen pribadi/sampingan dijalankan pada container terpisah (`openclaw-sandbox` dan `hermes-sandbox`) untuk mencegah kebocoran konteks dan dependensi.
- **Hard Cap Pengeluaran API:** Setiap konfigurasi API wajib memiliki batas maksimal pengeluaran (*hard cap*) dan pembatasan iterasi/retry agent untuk mencegah *infinite loop* yang menguras kuota/biaya.
- **Kebersihan Git:** File `.env`, database lokal (`*.db`, `*.sqlite`), seluruh isi `data/staging/*`, serta folder log/cache diblokir oleh `.gitignore`.
