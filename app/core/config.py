"""Manajemen konfigurasi"""

import os
from pathlib import Path
from typing import Optional, List
from pydantic_settings import BaseSettings


# Direktori root proyek
ROOT_DIR = Path(__file__).parents[2]

# Path file .env (mendukung kustomisasi melalui environment variable)
ENV_FILE_PATH = Path(os.getenv("ENV_FILE_PATH", ROOT_DIR / ".env"))


class Settings(BaseSettings):
    """Konfigurasi aplikasi

    Prioritas konfigurasi: Environment variable > File .env > Nilai default
    """

    # Konfigurasi server
    HOST: str = "0.0.0.0"
    PORT: int = 9563
    DEBUG: bool = False

    # API key (untuk melindungi gateway ini)
    API_KEY: str = ""

    # Konfigurasi proxy (opsional) - Mendukung http/https/socks5
    PROXY_URL: Optional[str] = None  # Contoh: http://127.0.0.1:7890 atau socks5://127.0.0.1:1080

    # HTTP proxy (untuk library requests)
    HTTP_PROXY: Optional[str] = None
    HTTPS_PROXY: Optional[str] = None

    # Cookie verifikasi Cloudflare (untuk request verifikasi usia)
    CF_CLEARANCE: str = ""

    # Jumlah default pembuatan gambar (1-4)
    DEFAULT_IMAGE_COUNT: int = 4

    # Konfigurasi SSO
    SSO_FILE: Path = ROOT_DIR / "key.txt"

    # Penyimpanan gambar (opsional, untuk caching)
    IMAGES_DIR: Path = ROOT_DIR / "data" / "images"
    VIDEOS_DIR: Path = ROOT_DIR / "data" / "videos"
    BASE_URL: Optional[str] = None  # Untuk generate URL gambar, jika tidak diatur otomatis menggunakan HOST:PORT

    # Konfigurasi generate
    DEFAULT_ASPECT_RATIO: str = "2:3"  # Rasio aspek default
    GENERATION_TIMEOUT: int = 120  # Timeout generate (detik)

    # Alamat WebSocket Grok resmi (nilai tetap, tidak perlu konfigurasi)
    GROK_WS_URL: str = "wss://grok.com/ws/imagine/listen"

    # Konfigurasi Redis (untuk persistensi status rotasi SSO)
    REDIS_ENABLED: bool = False  # Apakah mengaktifkan Redis
    REDIS_URL: str = "redis://localhost:6379/0"  # URL koneksi Redis

    # Konfigurasi rotasi SSO
    SSO_ROTATION_STRATEGY: str = "hybrid"  # Strategi rotasi: round_robin/least_used/least_recent/weighted/hybrid
    SSO_DAILY_LIMIT: int = 10  # Batas jumlah per key setiap 24 jam

    def get_base_url(self) -> str:
        """Mendapatkan URL dasar gambar, jika tidak diatur akan dibuat otomatis dari HOST:PORT"""
        if self.BASE_URL:
            return self.BASE_URL
        host = "127.0.0.1" if self.HOST == "0.0.0.0" else self.HOST
        return f"http://{host}:{self.PORT}"

    class Config:
        env_file = str(ENV_FILE_PATH)
        env_file_encoding = "utf-8"
        extra = "ignore"  # Abaikan environment variable yang tidak terdefinisi

    def get_proxy_dict(self) -> Optional[dict]:
        """Mendapatkan dictionary konfigurasi proxy (untuk requests)"""
        if self.PROXY_URL:
            return {
                "http": self.PROXY_URL,
                "https": self.PROXY_URL
            }
        if self.HTTP_PROXY or self.HTTPS_PROXY:
            return {
                "http": self.HTTP_PROXY,
                "https": self.HTTPS_PROXY
            }
        return None


def _ensure_env_file():
    """Memastikan file .env ada, jika tidak ada maka buat template default"""
    if not ENV_FILE_PATH.exists():
        # Pastikan direktori parent ada
        ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Buat file .env default
        default_env = """# File Konfigurasi Grok Imagine API Gateway
# Prioritas konfigurasi: Environment variable > File .env > Nilai default

# ============ Konfigurasi Server ============
HOST=0.0.0.0
PORT=9563
# Mode DEBUG: true untuk menyimpan log ke log.txt, false untuk tidak menyimpan
DEBUG=false

# ============ Keamanan API ============
API_KEY=your-secure-api-key-here

# ============ Konfigurasi Proxy ============
# Mendukung proxy http/https/socks5, hapus komentar dan isi alamat proxy Anda
# PROXY_URL=http://127.0.0.1:7890
# PROXY_URL=socks5://127.0.0.1:1080

# ============ Verifikasi Cloudflare ============
# Cookie cf_clearance, digunakan untuk request verifikasi usia, dapatkan dari browser
CF_CLEARANCE=

# ============ Konfigurasi Pembuatan Gambar ============
# Jumlah default pembuatan gambar (1-4)
DEFAULT_IMAGE_COUNT=4

# ============ Konfigurasi SSO ============
# Path file kunci SSO (satu token per baris)
SSO_FILE=key.txt

# ============ Penyimpanan Gambar ============
# Catatan: Jika BASE_URL tidak diatur, akan dibuat otomatis dari HOST:PORT
# Jika diakses melalui reverse proxy atau domain, isi alamat akses eksternal yang sebenarnya
# BASE_URL=http://your-domain.com
# Folder video lokal (opsional)
# VIDEOS_DIR=data/videos

# ============ Konfigurasi Generate ============
DEFAULT_ASPECT_RATIO=2:3
GENERATION_TIMEOUT=120

# ============ Konfigurasi Redis ============
# Setelah mengaktifkan Redis, status SSO akan dipersisten, mendukung deployment terdistribusi
# REDIS_ENABLED=true
# REDIS_URL=redis://localhost:6379/0

# ============ Konfigurasi Rotasi SSO ============
# Strategi rotasi: round_robin(rotasi sederhana) / least_used(paling sedikit digunakan) / least_recent(paling lama tidak digunakan) / weighted(berbobot) / hybrid(gabungan direkomendasikan)
# SSO_ROTATION_STRATEGY=hybrid
# Batas jumlah panggilan per key setiap 24 jam
# SSO_DAILY_LIMIT=10
"""
        ENV_FILE_PATH.write_text(default_env, encoding="utf-8")


# Pastikan file .env ada
_ensure_env_file()

# Buat instance konfigurasi global
settings = Settings()
