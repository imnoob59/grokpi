"""SSO Key Manager - Versi Redis

Fitur yang didukung:
1. Batas jumlah penggunaan per key (10 kali per 24 jam)
2. Pencatatan waktu penggunaan terakhir
3. Berbagai strategi rotasi
4. Status persisten (tidak hilang saat restart)
5. Dukungan terdistribusi (deployment multi-instance)
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, List, Dict, Any
from enum import Enum
from app.core.config import settings
from app.core.logger import logger

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None
    logger.warning("[SSO] Library redis tidak terinstal, akan menggunakan mode memori")


class RotationStrategy(Enum):
    """Strategi rotasi"""
    ROUND_ROBIN = "round_robin"        # Rotasi sederhana
    LEAST_USED = "least_used"          # Prioritas paling sedikit digunakan
    LEAST_RECENT = "least_recent"      # Prioritas paling lama tidak digunakan
    WEIGHTED = "weighted"              # Rotasi berbobot (berdasarkan sisa kuota)
    HYBRID = "hybrid"                  # Strategi gabungan (direkomendasikan)


class RedisSSOManager:
    """SSO Key Manager versi Redis

    Struktur data Redis:
    - sso:keys              -> Set: Semua SSO key yang tersedia
    - sso:failed            -> Set: SSO key yang gagal saat ini
    - sso:usage:{key_hash}  -> Hash: {count: int, last_used: timestamp, first_used: timestamp}
    - sso:index             -> String: Indeks rotasi saat ini (untuk round_robin)
    - sso:daily_reset       -> String: Timestamp reset terakhir
    """

    # Konfigurasi
    DAILY_LIMIT = 10           # Batas jumlah per key setiap 24 jam
    RESET_INTERVAL = 86400     # 24 jam (detik)

    # Prefix key Redis
    PREFIX = "sso:"
    KEYS_SET = f"{PREFIX}keys"
    FAILED_SET = f"{PREFIX}failed"
    INDEX_KEY = f"{PREFIX}index"
    DAILY_RESET_KEY = f"{PREFIX}daily_reset"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        strategy: RotationStrategy = RotationStrategy.HYBRID,
        daily_limit: int = 10
    ):
        self.redis_url = redis_url
        self.strategy = strategy
        self.DAILY_LIMIT = daily_limit
        self._redis = None
        self._lock = asyncio.Lock()
        self._sso_list: List[str] = []  # Cache lokal
        self._initialized = False

    async def _get_redis(self):
        """Mendapatkan koneksi Redis"""
        if self._redis is None:
            self._redis = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    def _key_hash(self, sso: str) -> str:
        """Generate hash pendek untuk key (untuk key Redis)"""
        import hashlib
        return hashlib.md5(sso.encode()).hexdigest()[:12]

    def _usage_key(self, sso: str) -> str:
        """Mendapatkan key Redis statistik penggunaan untuk SSO tertentu"""
        return f"{self.PREFIX}usage:{self._key_hash(sso)}"

    async def initialize(self) -> int:
        """Inisialisasi: Memuat daftar SSO ke Redis"""
        async with self._lock:
            if self._initialized:
                return len(self._sso_list)

            # Muat dari file
            self._sso_list = self._load_from_file()
            if not self._sso_list:
                return 0

            r = await self._get_redis()

            # Cek apakah perlu reset harian
            await self._check_daily_reset(r)

            # Sinkronkan ke Redis
            pipe = r.pipeline()
            pipe.delete(self.KEYS_SET)
            for sso in self._sso_list:
                pipe.sadd(self.KEYS_SET, sso)
                # Inisialisasi statistik penggunaan (jika belum ada)
                usage_key = self._usage_key(sso)
                pipe.hsetnx(usage_key, "count", 0)
                pipe.hsetnx(usage_key, "last_used", 0)
                pipe.hsetnx(usage_key, "first_used", int(time.time()))
                pipe.hsetnx(usage_key, "age_verified", 0)
            await pipe.execute()

            self._initialized = True
            logger.info(f"[SSO-Redis] Inisialisasi selesai, memuat {len(self._sso_list)} SSO")
            return len(self._sso_list)

    def _load_from_file(self) -> List[str]:
        """Memuat daftar SSO dari file"""
        sso_list = []
        sso_file = settings.SSO_FILE

        if not sso_file.exists():
            logger.warning(f"[SSO-Redis] File tidak ada: {sso_file}")
            return sso_list

        with open(sso_file, 'r', encoding='utf-8') as f:
            for line in f:
                sso = line.strip()
                if sso and not sso.startswith('#'):
                    sso_list.append(sso)

        return sso_list

    async def _check_daily_reset(self, r):
        """Cek dan jalankan reset harian"""
        now = int(time.time())
        last_reset = await r.get(self.DAILY_RESET_KEY)

        if last_reset is None:
            # Pertama kali dijalankan
            await r.set(self.DAILY_RESET_KEY, now)
            return

        last_reset = int(last_reset)
        if now - last_reset >= self.RESET_INTERVAL:
            logger.info("[SSO-Redis] Menjalankan reset harian...")
            # Reset jumlah penggunaan semua key
            for sso in self._sso_list:
                usage_key = self._usage_key(sso)
                await r.hset(usage_key, "count", 0)
            # Kosongkan daftar gagal
            await r.delete(self.FAILED_SET)
            # Update waktu reset
            await r.set(self.DAILY_RESET_KEY, now)
            logger.info("[SSO-Redis] Reset harian selesai")

    async def get_next_sso(self) -> Optional[str]:
        """Mendapatkan SSO berikutnya yang tersedia"""
        if not self._initialized:
            await self.initialize()

        if not self._sso_list:
            return None

        r = await self._get_redis()

        # Cek reset harian
        await self._check_daily_reset(r)

        # Pilih berdasarkan strategi
        if self.strategy == RotationStrategy.ROUND_ROBIN:
            return await self._get_round_robin(r)
        elif self.strategy == RotationStrategy.LEAST_USED:
            return await self._get_least_used(r)
        elif self.strategy == RotationStrategy.LEAST_RECENT:
            return await self._get_least_recent(r)
        elif self.strategy == RotationStrategy.WEIGHTED:
            return await self._get_weighted(r)
        else:  # HYBRID
            return await self._get_hybrid(r)

    async def _get_available_keys(self, r) -> List[str]:
        """Mendapatkan semua key yang tersedia (tidak gagal dan tidak melebihi batas)"""
        failed = await r.smembers(self.FAILED_SET)
        available = []

        for sso in self._sso_list:
            if sso in failed:
                continue

            # Cek jumlah penggunaan
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            if count >= self.DAILY_LIMIT:
                continue

            available.append(sso)

        return available

    async def _get_round_robin(self, r) -> Optional[str]:
        """Rotasi sederhana"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # Dapatkan dan increment indeks
        index = await r.incr(self.INDEX_KEY)
        index = (index - 1) % len(available)

        return available[index]

    async def _get_least_used(self, r) -> Optional[str]:
        """Prioritas paling sedikit digunakan"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # Dapatkan yang paling sedikit digunakan
        min_count = float('inf')
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            if count < min_count:
                min_count = count
                selected = sso

        return selected

    async def _get_least_recent(self, r) -> Optional[str]:
        """Prioritas paling lama tidak digunakan"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # Dapatkan yang paling lama tidak digunakan
        oldest_time = float('inf')
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            last_used = int(usage.get("last_used", 0))
            if last_used < oldest_time:
                oldest_time = last_used
                selected = sso

        return selected

    async def _get_weighted(self, r) -> Optional[str]:
        """Rotasi berbobot (sisa kuota sebagai bobot)"""
        import random

        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # Hitung bobot
        weights = []
        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            remaining = self.DAILY_LIMIT - count
            weights.append(max(1, remaining))  # Minimal 1

        # Pilih acak berbobot
        total = sum(weights)
        r_val = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r_val <= cumulative:
                return available[i]

        return available[-1]

    async def _get_hybrid(self, r) -> Optional[str]:
        """Strategi gabungan: Mempertimbangkan sisa kuota dan waktu penggunaan terakhir

        Formula skor: score = remaining_quota * time_factor
        - remaining_quota: Sisa kuota (1-10)
        - time_factor: Faktor waktu, semakin lama sejak penggunaan terakhir skor semakin tinggi
        """
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        now = time.time()
        best_score = -1
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            last_used = int(usage.get("last_used", 0))

            remaining = self.DAILY_LIMIT - count
            # Faktor waktu: setiap menit +0.1 poin, maksimal +10 poin
            if last_used == 0:
                time_factor = 10  # Belum pernah digunakan, beri skor tertinggi
            else:
                minutes_ago = (now - last_used) / 60
                time_factor = min(10, minutes_ago * 0.1)

            score = remaining * (1 + time_factor)

            if score > best_score:
                best_score = score
                selected = sso

        return selected

    async def _handle_all_exhausted(self, r) -> Optional[str]:
        """Menangani situasi ketika semua key habis"""
        logger.warning("[SSO-Redis] Semua SSO sudah habis atau gagal")

        # Cek apakah semua key tidak tersedia karena gagal
        failed = await r.smembers(self.FAILED_SET)
        if len(failed) == len(self._sso_list):
            # Semua key gagal, reset daftar gagal
            await r.delete(self.FAILED_SET)
            logger.info("[SSO-Redis] Reset daftar gagal")
            return self._sso_list[0] if self._sso_list else None

        # Jika tidak, kuota habis, return None
        return None

    async def record_usage(self, sso: str):
        """Catat penggunaan (update statistik setelah dipanggil)"""
        r = await self._get_redis()
        usage_key = self._usage_key(sso)
        now = int(time.time())

        pipe = r.pipeline()
        pipe.hincrby(usage_key, "count", 1)
        pipe.hset(usage_key, "last_used", now)
        await pipe.execute()

        logger.debug(f"[SSO-Redis] Catat penggunaan: {sso[:20]}...")

    async def mark_failed(self, sso: str, reason: str = ""):
        """Tandai SSO sebagai gagal"""
        r = await self._get_redis()
        await r.sadd(self.FAILED_SET, sso)
        logger.warning(f"[SSO-Redis] Tandai gagal: {sso[:20]}... Alasan: {reason}")

    async def mark_success(self, sso: str):
        """Tandai SSO sebagai berhasil (hapus dari daftar gagal)"""
        r = await self._get_redis()
        await r.srem(self.FAILED_SET, sso)

    async def get_age_verified(self, sso: str) -> int:
        """Mendapatkan status verifikasi usia (0=belum terverifikasi, 1=sudah terverifikasi)"""
        r = await self._get_redis()
        usage_key = self._usage_key(sso)
        age_verified = await r.hget(usage_key, "age_verified")
        return int(age_verified) if age_verified else 0

    async def set_age_verified(self, sso: str, verified: int = 1):
        """Mengatur status verifikasi usia"""
        r = await self._get_redis()
        usage_key = self._usage_key(sso)
        await r.hset(usage_key, "age_verified", verified)
        logger.info(f"[SSO-Redis] Atur status verifikasi usia: {sso[:20]}... -> {verified}")

    async def get_status(self) -> Dict[str, Any]:
        """Mendapatkan status detail"""
        if not self._initialized:
            await self.initialize()

        r = await self._get_redis()
        failed = await r.smembers(self.FAILED_SET)

        keys_status = []
        for sso in self._sso_list:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            last_used = int(usage.get("last_used", 0))

            keys_status.append({
                "key_prefix": sso[:20] + "...",
                "used_today": count,
                "remaining": max(0, self.DAILY_LIMIT - count),
                "last_used": last_used,
                "failed": sso in failed
            })

        # Dapatkan waktu reset berikutnya
        last_reset = await r.get(self.DAILY_RESET_KEY)
        next_reset = int(last_reset or 0) + self.RESET_INTERVAL

        return {
            "total_keys": len(self._sso_list),
            "failed_count": len(failed),
            "strategy": self.strategy.value,
            "daily_limit": self.DAILY_LIMIT,
            "next_reset_timestamp": next_reset,
            "keys": keys_status
        }

    async def reload(self) -> int:
        """Muat ulang daftar SSO"""
        async with self._lock:
            self._initialized = False
            self._sso_list = []
            r = await self._get_redis()
            await r.delete(self.KEYS_SET)
            return await self.initialize()

    async def reset_daily_usage(self):
        """Reset manual jumlah penggunaan harian"""
        r = await self._get_redis()
        for sso in self._sso_list:
            await r.hset(self._usage_key(sso), "count", 0)
        await r.delete(self.FAILED_SET)
        await r.set(self.DAILY_RESET_KEY, int(time.time()))
        logger.info("[SSO-Redis] Reset manual jumlah penggunaan harian selesai")

    async def close(self):
        """Tutup koneksi Redis"""
        if self._redis:
            await self._redis.close()
            self._redis = None


# Fungsi factory: Tentukan manager mana yang digunakan berdasarkan konfigurasi
def create_sso_manager(
    use_redis: bool = True,
    redis_url: str = "redis://localhost:6379/0",
    strategy: str = "hybrid",
    daily_limit: int = 10
):
    """Buat SSO manager

    Args:
        use_redis: Apakah menggunakan Redis (jika tidak gunakan versi memori)
        redis_url: URL koneksi Redis
        strategy: Strategi rotasi (round_robin/least_used/least_recent/weighted/hybrid)
        daily_limit: Batas jumlah per key setiap hari
    """
    if use_redis and REDIS_AVAILABLE:
        return RedisSSOManager(
            redis_url=redis_url,
            strategy=RotationStrategy(strategy),
            daily_limit=daily_limit
        )
    else:
        # Fallback ke versi file
        from app.services.sso_manager import SSOManager
        logger.warning("[SSO] Menggunakan manager versi file")
        return SSOManager(strategy=strategy, daily_limit=daily_limit)
