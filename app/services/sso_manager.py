"""SSO Key Manager - Versi File (mendukung berbagai strategi rotasi)

Fitur yang didukung:
1. Batas jumlah penggunaan per key (10 kali per 24 jam)
2. Pencatatan waktu penggunaan terakhir
3. Berbagai strategi rotasi
4. Status disimpan ke file JSON (tidak hilang saat restart)
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass, field, asdict
from app.core.config import settings
from app.core.logger import logger


class RotationStrategy(Enum):
    """Strategi rotasi"""
    ROUND_ROBIN = "round_robin"        # Rotasi sederhana
    LEAST_USED = "least_used"          # Prioritas paling sedikit digunakan
    LEAST_RECENT = "least_recent"      # Prioritas paling lama tidak digunakan
    WEIGHTED = "weighted"              # Rotasi berbobot (berdasarkan sisa kuota)
    HYBRID = "hybrid"                  # Strategi gabungan (direkomendasikan)


@dataclass
class KeyUsage:
    """Statistik penggunaan untuk satu key"""
    count: int = 0              # Jumlah penggunaan hari ini
    last_used: float = 0        # Timestamp penggunaan terakhir
    first_used: float = 0       # Timestamp penggunaan pertama
    failed: bool = False        # Apakah ditandai sebagai gagal
    age_verified: int = 0       # Apakah usia sudah terverifikasi (0=belum, 1=sudah)


class SSOManager:
    """SSO Key Manager - Mendukung berbagai strategi rotasi

    Memuat token dari file SSO_FILE, satu per baris
    Status disimpan ke file JSON untuk persistensi
    """

    # Konfigurasi
    RESET_INTERVAL = 86400     # 24 jam (detik)

    def __init__(
        self,
        strategy: str = "hybrid",
        daily_limit: int = 10
    ):
        self._sso_list: List[str] = []
        self._current_index: int = 0
        self._lock = asyncio.Lock()
        self._usage: Dict[str, KeyUsage] = {}
        self._last_reset: float = 0
        self.strategy = RotationStrategy(strategy)
        self.daily_limit = daily_limit
        self._state_file = settings.SSO_FILE.parent / "sso_state.json"

    def _key_hash(self, sso: str) -> str:
        """Generate hash pendek untuk key"""
        import hashlib
        return hashlib.md5(sso.encode()).hexdigest()[:12]

    def load_sso_list(self) -> int:
        """Memuat daftar SSO dari file"""
        self._sso_list = []

        sso_file = settings.SSO_FILE
        if not sso_file.exists():
            logger.warning(f"[SSO] File tidak ada: {sso_file}")
            return 0

        with open(sso_file, 'r', encoding='utf-8') as f:
            for line in f:
                sso = line.strip()
                if sso and not sso.startswith('#'):
                    self._sso_list.append(sso)
                    # Inisialisasi statistik penggunaan
                    if sso not in self._usage:
                        self._usage[sso] = KeyUsage(first_used=time.time())

        # Muat status persisten
        self._load_state()

        logger.info(f"[SSO] Memuat {len(self._sso_list)} SSO dari file, strategi: {self.strategy.value}")
        return len(self._sso_list)

    def _load_state(self):
        """Memuat status dari file"""
        if not self._state_file.exists():
            return

        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._last_reset = data.get("last_reset", 0)
            self._current_index = data.get("current_index", 0)

            # Cek apakah perlu reset harian
            if time.time() - self._last_reset >= self.RESET_INTERVAL:
                self._do_daily_reset()
            else:
                # Pulihkan statistik penggunaan
                for key_hash, usage_data in data.get("usage", {}).items():
                    # Temukan sso yang sesuai
                    for sso in self._sso_list:
                        if self._key_hash(sso) == key_hash:
                            self._usage[sso] = KeyUsage(**usage_data)
                            break

            logger.info("[SSO] Status persisten telah dimuat")
        except Exception as e:
            logger.warning(f"[SSO] Gagal memuat status: {e}")

    def _save_state(self):
        """Simpan status ke file"""
        try:
            usage_data = {}
            for sso, usage in self._usage.items():
                usage_data[self._key_hash(sso)] = asdict(usage)

            data = {
                "last_reset": self._last_reset,
                "current_index": self._current_index,
                "usage": usage_data
            }

            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"[SSO] Gagal menyimpan status: {e}")

    def _do_daily_reset(self):
        """Jalankan reset harian"""
        logger.info("[SSO] Menjalankan reset harian...")
        for sso in self._sso_list:
            if sso in self._usage:
                self._usage[sso].count = 0
                self._usage[sso].failed = False
        self._last_reset = time.time()
        self._save_state()
        logger.info("[SSO] Reset harian selesai")

    def _check_daily_reset(self):
        """Cek apakah perlu reset harian"""
        if self._last_reset == 0:
            self._last_reset = time.time()
            return

        if time.time() - self._last_reset >= self.RESET_INTERVAL:
            self._do_daily_reset()

    def _get_available_keys(self) -> List[str]:
        """Mendapatkan semua key yang tersedia (tidak gagal dan tidak melebihi batas)"""
        available = []
        for sso in self._sso_list:
            usage = self._usage.get(sso, KeyUsage())
            if usage.failed:
                continue
            if usage.count >= self.daily_limit:
                continue
            available.append(sso)
        return available

    async def get_next_sso(self) -> Optional[str]:
        """Mendapatkan SSO berikutnya yang tersedia"""
        async with self._lock:
            if not self._sso_list:
                self.load_sso_list()

            if not self._sso_list:
                return None

            # Cek reset harian
            self._check_daily_reset()

            # Pilih berdasarkan strategi
            if self.strategy == RotationStrategy.ROUND_ROBIN:
                return self._get_round_robin()
            elif self.strategy == RotationStrategy.LEAST_USED:
                return self._get_least_used()
            elif self.strategy == RotationStrategy.LEAST_RECENT:
                return self._get_least_recent()
            elif self.strategy == RotationStrategy.WEIGHTED:
                return self._get_weighted()
            else:  # HYBRID
                return self._get_hybrid()

    def _get_round_robin(self) -> Optional[str]:
        """Rotasi sederhana"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        # Pastikan indeks dalam rentang
        self._current_index = self._current_index % len(available)
        selected = available[self._current_index]
        self._current_index = (self._current_index + 1) % len(available)
        return selected

    def _get_least_used(self) -> Optional[str]:
        """Prioritas paling sedikit digunakan"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        min_count = float('inf')
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            if usage.count < min_count:
                min_count = usage.count
                selected = sso

        return selected

    def _get_least_recent(self) -> Optional[str]:
        """Prioritas paling lama tidak digunakan"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        oldest_time = float('inf')
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            if usage.last_used < oldest_time:
                oldest_time = usage.last_used
                selected = sso

        return selected

    def _get_weighted(self) -> Optional[str]:
        """Rotasi berbobot (sisa kuota sebagai bobot)"""
        import random

        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        weights = []
        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            remaining = self.daily_limit - usage.count
            weights.append(max(1, remaining))

        total = sum(weights)
        r_val = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r_val <= cumulative:
                return available[i]

        return available[-1]

    def _get_hybrid(self) -> Optional[str]:
        """Strategi gabungan: Mempertimbangkan sisa kuota dan waktu penggunaan terakhir"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        now = time.time()
        best_score = -1
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            remaining = self.daily_limit - usage.count

            if usage.last_used == 0:
                time_factor = 10
            else:
                minutes_ago = (now - usage.last_used) / 60
                time_factor = min(10, minutes_ago * 0.1)

            score = remaining * (1 + time_factor)

            if score > best_score:
                best_score = score
                selected = sso

        return selected

    def _handle_all_exhausted(self) -> Optional[str]:
        """Menangani situasi ketika semua key habis"""
        logger.warning("[SSO] Semua SSO sudah habis atau gagal")

        # Cek apakah semua key gagal
        all_failed = all(
            self._usage.get(sso, KeyUsage()).failed
            for sso in self._sso_list
        )

        if all_failed:
            # Reset status gagal
            for sso in self._sso_list:
                if sso in self._usage:
                    self._usage[sso].failed = False
            self._save_state()
            logger.info("[SSO] Reset daftar gagal")
            return self._sso_list[0] if self._sso_list else None

        return None

    async def record_usage(self, sso: str):
        """Catat penggunaan"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()

            self._usage[sso].count += 1
            self._usage[sso].last_used = time.time()
            self._save_state()
            logger.debug(f"[SSO] Catat penggunaan: {sso[:20]}... Jumlah hari ini: {self._usage[sso].count}")

    async def mark_failed(self, sso: str, reason: str = ""):
        """Tandai SSO sebagai gagal"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()
            self._usage[sso].failed = True
            self._save_state()
            logger.warning(f"[SSO] Tandai gagal: {sso[:20]}... Alasan: {reason}")

    async def mark_success(self, sso: str):
        """Tandai SSO sebagai berhasil (hapus dari daftar gagal)"""
        async with self._lock:
            if sso in self._usage:
                self._usage[sso].failed = False
                self._save_state()

    async def get_age_verified(self, sso: str) -> int:
        """Mendapatkan status verifikasi usia (0=belum terverifikasi, 1=sudah terverifikasi)"""
        async with self._lock:
            if sso in self._usage:
                return self._usage[sso].age_verified
            return 0

    async def set_age_verified(self, sso: str, verified: int = 1):
        """Mengatur status verifikasi usia"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()
            self._usage[sso].age_verified = verified
            self._save_state()
            logger.info(f"[SSO] Atur status verifikasi usia: {sso[:20]}... -> {verified}")

    def get_status(self) -> dict:
        """Mendapatkan status detail"""
        keys_status = []
        for sso in self._sso_list:
            usage = self._usage.get(sso, KeyUsage())
            keys_status.append({
                "key_prefix": sso[:20] + "...",
                "used_today": usage.count,
                "remaining": max(0, self.daily_limit - usage.count),
                "last_used": int(usage.last_used),
                "failed": usage.failed
            })

        next_reset = int(self._last_reset + self.RESET_INTERVAL) if self._last_reset else 0

        return {
            "total_keys": len(self._sso_list),
            "failed_count": sum(1 for u in self._usage.values() if u.failed),
            "strategy": self.strategy.value,
            "daily_limit": self.daily_limit,
            "next_reset_timestamp": next_reset,
            "keys": keys_status
        }

    async def reload(self) -> int:
        """Muat ulang daftar SSO"""
        async with self._lock:
            self._usage.clear()
            self._current_index = 0
            return self.load_sso_list()

    async def reset_daily_usage(self):
        """Reset manual jumlah penggunaan harian"""
        async with self._lock:
            self._do_daily_reset()
            logger.info("[SSO] Reset manual jumlah penggunaan harian selesai")


# Fungsi factory
def create_file_sso_manager(
    strategy: str = "hybrid",
    daily_limit: int = 10
) -> SSOManager:
    """Buat SSO manager versi file"""
    return SSOManager(strategy=strategy, daily_limit=daily_limit)


# Instance global (menggunakan konfigurasi)
sso_manager = SSOManager(
    strategy=settings.SSO_ROTATION_STRATEGY,
    daily_limit=settings.SSO_DAILY_LIMIT
)
