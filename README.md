# Grok Imagine API Gateway

Gateway API kompatibel OpenAI untuk **generate gambar dan video** menggunakan Grok, dengan cache media lokal, gallery modern, serta manajemen multi-SSO.

## Fitur Utama

- API OpenAI-style:
  - `POST /v1/images/generations`
  - `POST /v1/videos/generations`
  - `POST /v1/chat/completions`
- Generate image via WebSocket Grok (streaming progress didukung)
- Generate video via reverse flow (`media post` + `app-chat stream`)
- Auto download hasil media ke cache lokal:
  - Image ke `data/images`
  - Video ke `data/videos`
- Gallery modern:
  - `GET /gallery` (image)
  - `GET /video-gallery` (video)
- Delete media per item langsung dari gallery (image/video)
- Multi-SSO rotation strategy + retry/fallback
- Dukungan proxy HTTP/HTTPS/SOCKS5
- Dukungan Redis (opsional) untuk status rotasi SSO

## Requirement

- Python 3.10+
- Dependensi di `requirements.txt`

## Instalasi

```bash
pip install -r requirements.txt
```

## Konfigurasi

Aplikasi akan membuat file `.env` otomatis jika belum ada.

### Konfigurasi minimum

- Isi `key.txt` (1 token SSO per baris)
- Isi `.env`:

```env
HOST=0.0.0.0
PORT=9563
DEBUG=false

API_KEY=your-api-key
CF_CLEARANCE=your-cf-clearance
```

### Variabel penting

- `API_KEY` : Bearer token untuk akses API gateway
- `CF_CLEARANCE` : membantu flow reverse/age verification
- `PROXY_URL` / `HTTP_PROXY` / `HTTPS_PROXY` : proxy opsional
- `SSO_ROTATION_STRATEGY` : `round_robin|least_used|least_recent|weighted|hybrid`
- `SSO_DAILY_LIMIT` : limit harian penggunaan key

## Menjalankan Server

```bash
python main.py
```

Akses:

- Docs (Swagger): `http://127.0.0.1:9563/docs`
- ReDoc: `http://127.0.0.1:9563/redoc`
- Health: `http://127.0.0.1:9563/health`
- Image Gallery: `http://127.0.0.1:9563/gallery`
- Video Gallery: `http://127.0.0.1:9563/video-gallery`

## Autentikasi (Swagger & API)

Semua endpoint `/v1/*` menggunakan Bearer auth saat `API_KEY` di-set.

Header:

```http
Authorization: Bearer <API_KEY>
```

Di Swagger, gunakan tombol **Authorize** (ikon gembok), lalu isi token tanpa kata `Bearer`.

## Contoh Request

### 1) Generate Image

```bash
curl -X POST http://127.0.0.1:9563/v1/images/generations \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a cinematic portrait of a cat",
    "n": 1,
    "aspect_ratio": "9:16"
  }'
```

### 2) Generate Video

```bash
curl -X POST http://127.0.0.1:9563/v1/videos/generations \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "vertical cyberpunk alley, smooth camera movement",
    "aspect_ratio": "9:16",
    "duration_seconds": 6,
    "resolution": "480p",
    "preset": "normal"
  }'
```

### 3) Chat Completions (Image Workflow)

```bash
curl -X POST http://127.0.0.1:9563/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine",
    "messages": [{"role": "user", "content": "gambar seekor kucing imut"}],
    "stream": true
  }'
```

## Endpoint Ringkas

- `GET /` : informasi service
- `GET /health` : health check
- `GET /gallery` : gallery image
- `GET /video-gallery` : gallery video
- `GET /images/{filename}` : static image
- `GET /videos/{filename}` : static video
- `POST /v1/images/generations` : generate image
- `POST /v1/videos/generations` : generate video
- `POST /v1/chat/completions` : chat-compatible image generation
- `GET /admin/status` : status service/admin
- `DELETE /admin/media/image/{filename}` : hapus image by file
- `DELETE /admin/media/video/{filename}` : hapus video by file

## Struktur Cache Media

- `data/images` : file image cache
- `data/videos` : file video cache

## Catatan Operasional

- Jika video/image reverse flow kena 403, refresh `CF_CLEARANCE` dari browser.
- Jika menggunakan reverse proxy/domain, set `BASE_URL` agar URL media sesuai domain publik.
- Untuk deployment multi-instance, aktifkan Redis agar status rotasi token konsisten.

## Disclaimer

Gunakan sesuai kebijakan layanan upstream dan akun milik sendiri.
