"""Grok Imagine Image Generator - Menggunakan koneksi langsung WebSocket, mendukung preview streaming dan HTTP proxy"""

import asyncio
import json
import uuid
import time
import base64
import ssl
import re
import random
import string
from typing import Optional, List, Dict, Any, Callable, Awaitable
from dataclasses import dataclass, field

import aiohttp
from aiohttp_socks import ProxyConnector

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    curl_requests = None

from app.core.config import settings
from app.core.logger import logger

# Pilih SSO manager berdasarkan konfigurasi
if settings.REDIS_ENABLED:
    from app.services.redis_sso_manager import create_sso_manager
    sso_manager = create_sso_manager(
        use_redis=True,
        redis_url=settings.REDIS_URL,
        strategy=settings.SSO_ROTATION_STRATEGY,
        daily_limit=settings.SSO_DAILY_LIMIT
    )
else:
    from app.services.sso_manager import sso_manager


@dataclass
class ImageProgress:
    """Progress pembuatan untuk satu gambar"""
    image_id: str  # UUID yang diekstrak dari URL
    stage: str = "preview"  # preview -> medium -> final
    blob: str = ""
    blob_size: int = 0
    url: str = ""
    is_final: bool = False


@dataclass
class GenerationProgress:
    """Progress generate keseluruhan"""
    total: int = 4  # Jumlah yang diharapkan
    images: Dict[str, ImageProgress] = field(default_factory=dict)
    completed: int = 0  # Jumlah gambar final yang selesai
    has_medium: bool = False  # Apakah ada gambar tahap medium

    def get_completed_images(self) -> List[ImageProgress]:
        """Mendapatkan semua gambar yang selesai"""
        return [img for img in self.images.values() if img.is_final]

    def check_blocked(self) -> bool:
        """Cek apakah diblokir (ada medium tapi tidak ada final)"""
        has_medium = any(img.stage == "medium" for img in self.images.values())
        has_final = any(img.is_final for img in self.images.values())
        return has_medium and not has_final


# Tipe callback streaming
StreamCallback = Callable[[ImageProgress, GenerationProgress], Awaitable[None]]


class GrokImagineClient:
    """Klien WebSocket Grok Imagine"""

    MEDIA_POST_CREATE_URL = "https://grok.com/rest/media/post/create"
    APP_CHAT_NEW_URL = "https://grok.com/rest/app-chat/conversations/new"
    VIDEO_UPSCALE_URL = "https://grok.com/rest/media/video/upscale"

    def __init__(self):
        self._ssl_context = ssl.create_default_context()
        # Untuk ekstrak ID gambar dari URL
        self._url_pattern = re.compile(r'/images/([a-f0-9-]+)\.(png|jpg)')

    def _get_connector(self) -> Optional[aiohttp.BaseConnector]:
        """Mendapatkan connector (mendukung proxy)"""
        proxy_url = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY

        if proxy_url:
            logger.info(f"[Grok] Menggunakan proxy: {proxy_url}")
            # Mendukung proxy http/https/socks4/socks5
            return ProxyConnector.from_url(proxy_url, ssl=self._ssl_context)

        return aiohttp.TCPConnector(ssl=self._ssl_context)

    def _get_ws_headers(self, sso: str) -> Dict[str, str]:
        """Membangun header request WebSocket"""
        return {
            "Cookie": f"sso={sso}; sso-rw={sso}",
            "Origin": "https://grok.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _get_http_headers(self, sso: str, referer: str = "https://grok.com/") -> Dict[str, str]:
        """Membangun header request HTTP"""
        cookie = f"sso={sso}; sso-rw={sso}"
        if settings.CF_CLEARANCE:
            cookie += f"; cf_clearance={settings.CF_CLEARANCE}"

        statsig_message = self._generate_statsig_message()

        return {
            "Cookie": cookie,
            "Origin": "https://grok.com",
            "Referer": referer,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Baggage": "sentry-environment=production,sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c",
            "Sec-Ch-Ua": '"Google Chrome";v="133", "Chromium";v="133", "Not(A:Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Ch-Ua-Arch": "x86",
            "Sec-Ch-Ua-Bitness": "64",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=1, i",
            "x-xai-request-id": str(uuid.uuid4()),
            "x-statsig-id": statsig_message,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _generate_statsig_message(self) -> str:
        """Membuat x-statsig-id mirip client web"""
        if random.choice([True, False]):
            rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
            message = f"e:TypeError: Cannot read properties of null (reading 'children['{rand}']')"
        else:
            rand = "".join(random.choices(string.ascii_lowercase, k=10))
            message = f"e:TypeError: Cannot read properties of undefined (reading '{rand}')"
        return base64.b64encode(message.encode()).decode()

    def _extract_image_id(self, url: str) -> Optional[str]:
        """Ekstrak ID gambar dari URL"""
        match = self._url_pattern.search(url)
        if match:
            return match.group(1)
        return None

    def _is_final_image(self, url: str, blob_size: int) -> bool:
        """Menentukan apakah gambar final HD"""        
        # Versi final adalah format .jpg, ukuran biasanya > 100KB
        return url.endswith('.jpg') and blob_size > 100000

    def _build_generate_message(
        self,
        prompt: str,
        request_id: str,
        aspect_ratio: str,
        enable_nsfw: bool,
        media_mode: str = "image",
        duration_seconds: int = 6,
        resolution: str = "480p"
    ) -> Dict[str, Any]:
        properties: Dict[str, Any] = {
            "section_count": 0,
            "is_kids_mode": False,
            "enable_nsfw": enable_nsfw,
            "skip_upsampler": False,
            "is_initial": False,
            "aspect_ratio": aspect_ratio
        }

        if media_mode == "video":
            properties.update({
                "is_video": True,
                "mode": "video",
                "generation_type": "video",
                "output_type": "video",
                "duration_seconds": duration_seconds,
                "video_duration_seconds": duration_seconds,
                "duration": duration_seconds,
                "resolution": resolution,
                "video_resolution": resolution,
                "target_resolution": resolution
            })

        return {
            "type": "conversation.item.create",
            "timestamp": int(time.time() * 1000),
            "item": {
                "type": "message",
                "content": [{
                    "requestId": request_id,
                    "text": prompt,
                    "type": "input_text",
                    "properties": properties
                }]
            }
        }

    async def _verify_age(self, sso: str) -> bool:
        """Verifikasi usia - Menggunakan curl_cffi untuk mensimulasikan request browser"""
        if not CURL_CFFI_AVAILABLE:
            logger.warning("[Grok] curl_cffi tidak terinstal, lewati verifikasi usia")
            return False

        if not settings.CF_CLEARANCE:
            logger.warning("[Grok] CF_CLEARANCE tidak dikonfigurasi, lewati verifikasi usia")
            return False

        cookie_str = f"sso={sso}; sso-rw={sso}; cf_clearance={settings.CF_CLEARANCE}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "Accept": "*/*",
            "Cookie": cookie_str,
            "Content-Type": "application/json",
        }

        proxy = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY

        logger.info("[Grok] Sedang melakukan verifikasi usia...")

        try:
            # Jalankan request curl_cffi sinkron di thread pool
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: curl_requests.post(
                    "https://grok.com/rest/auth/set-birth-date",
                    headers=headers,
                    json={"birthDate": "2001-01-01T16:00:00.000Z"},
                    impersonate="chrome133a",
                    proxy=proxy,
                    verify=False
                )
            )

            if resp.status_code == 200:
                logger.info(f"[Grok] Verifikasi usia berhasil (status code: {resp.status_code})")
                return True
            else:
                logger.warning(f"[Grok] Response verifikasi usia: {resp.status_code} - {resp.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"[Grok] Verifikasi usia gagal: {e}")
            return False

    async def _create_video_post(self, session: aiohttp.ClientSession, sso: str, prompt: str) -> Optional[str]:
        """Buat media post untuk video generation"""
        payload = {
            "mediaType": "MEDIA_POST_TYPE_VIDEO",
            "prompt": prompt
        }
        headers = self._get_http_headers(sso, referer="https://grok.com")

        async with session.post(
            self.MEDIA_POST_CREATE_URL,
            json=payload,
            headers=headers,
            timeout=settings.GENERATION_TIMEOUT,
        ) as response:
            if response.status != 200:
                body = await response.text()
                logger.warning(f"[Grok-Video] media post gagal: {response.status} {body[:300]}")
                return None

            data = await response.json(content_type=None)
            post_id = data.get("post", {}).get("id")
            return post_id

    def _build_video_chat_payload(
        self,
        prompt: str,
        post_id: str,
        aspect_ratio: str,
        duration_seconds: int,
        resolution: str,
        preset: str = "normal"
    ) -> Dict[str, Any]:
        mode_map = {
            "fun": "--mode=extremely-crazy",
            "normal": "--mode=normal",
            "spicy": "--mode=extremely-spicy-or-crazy",
            "custom": "--mode=custom"
        }
        mode_flag = mode_map.get(preset, "--mode=normal")
        message = f"{prompt} {mode_flag}".strip()

        return {
            "deviceEnvInfo": {
                "darkModeEnabled": False,
                "devicePixelRatio": 2,
                "screenWidth": 1920,
                "screenHeight": 1080,
                "viewportWidth": 1920,
                "viewportHeight": 980,
            },
            "disableMemory": True,
            "disableSearch": False,
            "disableSelfHarmShortCircuit": False,
            "disableTextFollowUps": False,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "enableSideBySide": True,
            "fileAttachments": [],
            "forceConcise": False,
            "forceSideBySide": False,
            "imageAttachments": [],
            "imageGenerationCount": 2,
            "isAsyncChat": False,
            "isReasoning": False,
            "message": message,
            "modelMode": None,
            "modelName": "grok-3",
            "responseMetadata": {
                "requestModelDetails": {"modelId": "grok-3"},
                "modelConfigOverride": {
                    "modelMap": {
                        "videoGenModelConfig": {
                            "aspectRatio": aspect_ratio,
                            "parentPostId": post_id,
                            "resolutionName": resolution,
                            "videoLength": duration_seconds,
                        }
                    }
                }
            },
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "sendFinalMetadata": True,
            "temporary": True,
            "toolOverrides": {"videoGen": True},
        }

    def _extract_video_id(self, video_url: str) -> str:
        if not video_url:
            return ""
        match = re.search(r"/generated/([0-9a-fA-F-]{32,36})/", video_url)
        if match:
            return match.group(1)
        match = re.search(r"/([0-9a-fA-F-]{32,36})/generated_video", video_url)
        if match:
            return match.group(1)
        return ""

    async def _upscale_video_url(
        self,
        session: aiohttp.ClientSession,
        sso: str,
        video_url: str,
        resolution: str
    ) -> str:
        if resolution != "720p":
            return video_url

        video_id = self._extract_video_id(video_url)
        if not video_id:
            return video_url

        headers = self._get_http_headers(sso, referer="https://grok.com")
        payload = {"videoId": video_id}

        try:
            async with session.post(
                self.VIDEO_UPSCALE_URL,
                json=payload,
                headers=headers,
                timeout=settings.GENERATION_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return video_url
                data = await response.json(content_type=None)
                hd_url = data.get("hdMediaUrl") if isinstance(data, dict) else None
                return hd_url or video_url
        except Exception:
            return video_url

    def _sync_do_generate_video_via_curl(
        self,
        sso: str,
        prompt: str,
        aspect_ratio: str,
        duration_seconds: int,
        resolution: str,
        preset: str,
    ) -> Dict[str, Any]:
        """Flow video reverse via curl_cffi (sinkron, dipanggil lewat thread executor)."""
        if not CURL_CFFI_AVAILABLE:
            return {"success": False, "error": "curl_cffi tidak tersedia"}

        proxy = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY
        impersonates = ["chrome136", "chrome133a", "chrome131"]
        last_error: Dict[str, Any] = {"success": False, "error": "Video reverse flow gagal"}

        for impersonate in impersonates:
            result = self._sync_do_generate_video_via_curl_once(
                sso=sso,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                duration_seconds=duration_seconds,
                resolution=resolution,
                preset=preset,
                impersonate=impersonate,
                proxy=proxy,
            )
            if result.get("success"):
                return result

            last_error = result
            error_text = result.get("error", "")
            if "(403)" not in error_text and result.get("error_code") not in ["video_post_failed"]:
                return result

        return last_error

    def _sync_do_generate_video_via_curl_once(
        self,
        sso: str,
        prompt: str,
        aspect_ratio: str,
        duration_seconds: int,
        resolution: str,
        preset: str,
        impersonate: str,
        proxy: Optional[str],
    ) -> Dict[str, Any]:
        """Satu percobaan reverse video via curl_cffi."""

        # 1) media post create
        media_headers = self._get_http_headers(sso, referer="https://grok.com/imagine")
        media_payload = {
            "mediaType": "MEDIA_POST_TYPE_VIDEO",
            "prompt": prompt
        }

        media_resp = curl_requests.post(
            self.MEDIA_POST_CREATE_URL,
            headers=media_headers,
            json=media_payload,
            impersonate=impersonate,
            proxy=proxy,
            verify=False,
            timeout=settings.GENERATION_TIMEOUT,
        )

        if media_resp.status_code != 200:
            body_text = ""
            try:
                body_text = media_resp.text[:300]
            except Exception:
                body_text = ""
            return {
                "success": False,
                "error_code": "video_post_failed",
                "error": f"media_post failed ({media_resp.status_code}) {body_text}"
            }

        try:
            post_json = media_resp.json()
        except Exception:
            post_json = {}

        post_id = post_json.get("post", {}).get("id", "")
        if not post_id:
            return {
                "success": False,
                "error_code": "video_post_failed",
                "error": "Post ID tidak ditemukan"
            }

        # 2) app-chat stream
        payload = self._build_video_chat_payload(
            prompt=prompt,
            post_id=post_id,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            resolution=resolution,
            preset=preset,
        )
        chat_headers = self._get_http_headers(sso, referer="https://grok.com/imagine")

        chat_resp = curl_requests.post(
            self.APP_CHAT_NEW_URL,
            headers=chat_headers,
            json=payload,
            stream=True,
            impersonate=impersonate,
            proxy=proxy,
            verify=False,
            timeout=max(settings.GENERATION_TIMEOUT, 120),
        )

        if chat_resp.status_code != 200:
            error_code = ""
            if chat_resp.status_code == 429:
                error_code = "rate_limit_exceeded"
            elif chat_resp.status_code == 401:
                error_code = "unauthorized"
            body_text = ""
            try:
                body_text = chat_resp.text[:300]
            except Exception:
                body_text = ""
            return {
                "success": False,
                "error_code": error_code,
                "error": f"app_chat failed ({chat_resp.status_code}) {body_text}"
            }

        seen_types = set()
        preview_image_urls: List[str] = []
        final_video_url = ""
        final_thumbnail_url = ""

        for raw_line in chat_resp.iter_lines():
            if not raw_line:
                continue

            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="ignore").strip()
            else:
                line = str(raw_line).strip()

            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue

            try:
                data = json.loads(line)
            except Exception:
                continue

            resp = data.get("result", {}).get("response", {})
            if not isinstance(resp, dict):
                continue

            if resp.get("token"):
                seen_types.add("token")

            video_resp = resp.get("streamingVideoGenerationResponse", {})
            if isinstance(video_resp, dict) and video_resp:
                seen_types.add("streamingVideoGenerationResponse")
                progress = int(video_resp.get("progress", 0) or 0)
                video_url = video_resp.get("videoUrl", "")
                thumb_url = video_resp.get("thumbnailImageUrl", "")

                if thumb_url and thumb_url not in preview_image_urls:
                    preview_image_urls.append(thumb_url)

                if progress >= 100 and video_url:
                    final_video_url = video_url
                    final_thumbnail_url = thumb_url
                    break

        if final_video_url:
            return {
                "success": True,
                "urls": [final_video_url],
                "count": 1,
                "seen_types": sorted(list(seen_types)),
                "thumbnail_url": final_thumbnail_url,
            }

        return {
            "success": False,
            "error_code": "video_not_supported",
            "error": "Video progress/event tidak ditemukan dari app-chat stream",
            "seen_types": sorted(list(seen_types)),
            "image_preview_urls": preview_image_urls[:3],
        }

    async def generate_video(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        duration_seconds: int = 6,
        resolution: str = "480p",
        preset: str = "normal",
        enable_nsfw: bool = True,
        sso: Optional[str] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        last_error = None

        for attempt in range(max_retries):
            current_sso = sso if sso else await sso_manager.get_next_sso()

            if not current_sso:
                return {"success": False, "error": "Tidak ada SSO yang tersedia"}

            age_verified = await sso_manager.get_age_verified(current_sso)
            if age_verified == 0:
                verify_success = await self._verify_age(current_sso)
                if verify_success:
                    await sso_manager.set_age_verified(current_sso, 1)

            try:
                result = await self._do_generate_video(
                    sso=current_sso,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    duration_seconds=duration_seconds,
                    resolution=resolution,
                    preset=preset,
                    enable_nsfw=enable_nsfw
                )

                if result.get("success"):
                    await sso_manager.mark_success(current_sso)
                    if hasattr(sso_manager, 'record_usage'):
                        await sso_manager.record_usage(current_sso)
                    return result

                last_error = result
                error_code = result.get("error_code", "")
                if error_code in ["rate_limit_exceeded", "unauthorized"]:
                    await sso_manager.mark_failed(current_sso, result.get("error", ""))
                    if sso:
                        return result
                    logger.info(f"[Grok-Video] Percobaan {attempt + 1}/{max_retries} gagal, ganti SSO...")
                    continue

                return result

            except Exception as e:
                logger.error(f"[Grok-Video] Generate gagal: {e}")
                await sso_manager.mark_failed(current_sso, str(e))
                last_error = {"success": False, "error": str(e)}
                if sso:
                    return last_error

        return last_error or {"success": False, "error": "Semua retry gagal"}

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "2:3",
        n: int = None,
        enable_nsfw: bool = True,
        sso: Optional[str] = None,
        max_retries: int = 5,
        stream_callback: Optional[StreamCallback] = None
    ) -> Dict[str, Any]:
        """
        Generate gambar

        Args:
            prompt: Prompt
            aspect_ratio: Rasio aspek (1:1, 2:3, 3:2, 9:16, 16:9)
            n: Jumlah yang dihasilkan, jika tidak ditentukan gunakan nilai default konfigurasi
            enable_nsfw: Apakah mengaktifkan NSFW
            sso: SSO yang ditentukan, jika tidak maka ambil dari pool
            max_retries: Jumlah retry maksimal (untuk rotasi SSO berbeda)
            stream_callback: Callback streaming, dipanggil setiap kali ada update gambar

        Returns:
            Hasil generate, berisi daftar URL gambar
        """        
        # Gunakan jumlah gambar default dari konfigurasi
        if n is None:
            n = settings.DEFAULT_IMAGE_COUNT

        logger.info(f"[Grok] Request generate {n} gambar (DEFAULT_IMAGE_COUNT={settings.DEFAULT_IMAGE_COUNT})")

        last_error = None
        blocked_retries = 0  # Hitung retry blocked
        max_blocked_retries = 3  # Maksimal retry blocked

        for attempt in range(max_retries):
            current_sso = sso if sso else await sso_manager.get_next_sso()

            if not current_sso:
                return {"success": False, "error": "Tidak ada SSO yang tersedia"}

            # Cek status verifikasi usia
            age_verified = await sso_manager.get_age_verified(current_sso)
            if age_verified == 0:
                logger.info(f"[Grok] SSO {current_sso[:20]}... belum diverifikasi usia, mulai verifikasi...")
                verify_success = await self._verify_age(current_sso)
                if verify_success:
                    await sso_manager.set_age_verified(current_sso, 1)
                else:
                    logger.warning(f"[Grok] SSO {current_sso[:20]}... verifikasi usia gagal, lanjutkan mencoba generate")

            try:
                result = await self._do_generate(
                    sso=current_sso,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    n=n,
                    enable_nsfw=enable_nsfw,
                    stream_callback=stream_callback
                )

                if result.get("success"):
                    await sso_manager.mark_success(current_sso)
                    # Catat penggunaan (update statistik dalam mode Redis)
                    if hasattr(sso_manager, 'record_usage'):
                        await sso_manager.record_usage(current_sso)
                    return result

                error_code = result.get("error_code", "")

                # Cek apakah diblokir
                if error_code == "blocked":
                    blocked_retries += 1
                    logger.warning(
                        f"[Grok] Terdeteksi blocked, retry {blocked_retries}/{max_blocked_retries}"
                    )
                    await sso_manager.mark_failed(current_sso, "blocked - tidak dapat menghasilkan gambar final")

                    if blocked_retries >= max_blocked_retries:
                        return {
                            "success": False,
                            "error_code": "blocked",
                            "error": f"Berturut-turut {max_blocked_retries} kali diblokir, silakan coba lagi nanti"
                        }
                    # Jika SSO ditentukan maka tidak retry
                    if sso:
                        return result
                    continue

                if error_code in ["rate_limit_exceeded", "unauthorized"]:
                    await sso_manager.mark_failed(current_sso, result.get("error", ""))
                    last_error = result
                    if sso:
                        return result
                    logger.info(f"[Grok] Percobaan {attempt + 1}/{max_retries} gagal, ganti SSO...")
                    continue
                else:
                    return result

            except Exception as e:
                logger.error(f"[Grok] Generate gagal: {e}")
                await sso_manager.mark_failed(current_sso, str(e))
                last_error = {"success": False, "error": str(e)}
                if sso:
                    return last_error
                continue

        return last_error or {"success": False, "error": "Semua retry gagal"}

    async def _do_generate(
        self,
        sso: str,
        prompt: str,
        aspect_ratio: str,
        n: int,
        enable_nsfw: bool,
        stream_callback: Optional[StreamCallback] = None
    ) -> Dict[str, Any]:
        """Eksekusi generate"""
        request_id = str(uuid.uuid4())
        headers = self._get_ws_headers(sso)

        logger.info(f"[Grok] Koneksi WebSocket: {settings.GROK_WS_URL}")

        connector = self._get_connector()

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(
                    settings.GROK_WS_URL,
                    headers=headers,
                    heartbeat=20,
                    receive_timeout=settings.GENERATION_TIMEOUT
                ) as ws:
                    # Kirim request generate
                    message = self._build_generate_message(
                        prompt=prompt,
                        request_id=request_id,
                        aspect_ratio=aspect_ratio,
                        enable_nsfw=enable_nsfw,
                        media_mode="image"
                    )

                    await ws.send_json(message)
                    logger.info(f"[Grok] Request terkirim: {prompt[:50]}...")

                    # Pelacakan progress
                    progress = GenerationProgress(total=n)
                    error_info = None
                    start_time = time.time()
                    last_activity = time.time()
                    medium_received_time = None  # Waktu menerima medium

                    while time.time() - start_time < settings.GENERATION_TIMEOUT:
                        try:
                            ws_msg = await asyncio.wait_for(ws.receive(), timeout=5.0)

                            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                                last_activity = time.time()
                                msg = json.loads(ws_msg.data)
                                msg_type = msg.get("type")

                                if msg_type == "image":
                                    blob = msg.get("blob", "")
                                    url = msg.get("url", "")

                                    if blob and url:
                                        image_id = self._extract_image_id(url)
                                        if not image_id:
                                            continue

                                        blob_size = len(blob)
                                        is_final = self._is_final_image(url, blob_size)

                                        # Tentukan tahap
                                        if is_final:
                                            stage = "final"
                                        elif blob_size > 30000:
                                            stage = "medium"
                                            # Catat waktu menerima medium
                                            if medium_received_time is None:
                                                medium_received_time = time.time()
                                        else:
                                            stage = "preview"

                                        # Update atau buat image progress
                                        img_progress = ImageProgress(
                                            image_id=image_id,
                                            stage=stage,
                                            blob=blob,
                                            blob_size=blob_size,
                                            url=url,
                                            is_final=is_final
                                        )

                                        # Hanya update ke tahap lebih tinggi
                                        existing = progress.images.get(image_id)
                                        if not existing or (not existing.is_final):
                                            progress.images[image_id] = img_progress

                                            # Update hitungan selesai
                                            progress.completed = len([
                                                img for img in progress.images.values()
                                                if img.is_final
                                            ])

                                            logger.info(
                                                f"[Grok] Gambar {image_id[:8]}... "
                                                f"tahap={stage} ukuran={blob_size} "
                                                f"progress={progress.completed}/{n}"
                                            )

                                            # Panggil callback streaming
                                            if stream_callback:
                                                try:
                                                    await stream_callback(img_progress, progress)
                                                except Exception as e:
                                                    logger.warning(f"[Grok] Error callback streaming: {e}")

                                elif msg_type == "error":
                                    error_code = msg.get("err_code", "")
                                    error_msg = msg.get("err_msg", "")
                                    logger.warning(f"[Grok] Error: {error_code} - {error_msg}")
                                    error_info = {"error_code": error_code, "error": error_msg}

                                    if error_code == "rate_limit_exceeded":
                                        return {
                                            "success": False,
                                            "error_code": error_code,
                                            "error": error_msg
                                        }

                                # Cek apakah sudah terkumpul cukup gambar final
                                if progress.completed >= n:
                                    logger.info(f"[Grok] Sudah terkumpul {progress.completed} gambar final")
                                    break

                                # Cek apakah diblokir: ada medium tapi lebih dari 15 detik tidak ada final
                                if medium_received_time and progress.completed == 0:
                                    time_since_medium = time.time() - medium_received_time
                                    if time_since_medium > 15:
                                        logger.warning(
                                            f"[Grok] Terdeteksi blocked: setelah menerima medium "
                                            f"{time_since_medium:.1f}s masih tidak ada final"
                                        )
                                        return {
                                            "success": False,
                                            "error_code": "blocked",
                                            "error": "Generate diblokir, tidak dapat mendapatkan gambar final"
                                        }

                            elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                logger.warning(f"[Grok] WebSocket ditutup atau error: {ws_msg.type}")
                                break

                        except asyncio.TimeoutError:
                            # Cek apakah diblokir
                            if medium_received_time and progress.completed == 0:
                                time_since_medium = time.time() - medium_received_time
                                if time_since_medium > 10:
                                    logger.warning(
                                        f"[Grok] Timeout terdeteksi blocked: setelah menerima medium "
                                        f"{time_since_medium:.1f}s masih tidak ada final"
                                    )
                                    return {
                                        "success": False,
                                        "error_code": "blocked",
                                        "error": "Generate diblokir, tidak dapat mendapatkan gambar final"
                                    }

                            # Jika sudah ada beberapa gambar final dan lebih dari 10 detik tidak ada pesan baru, anggap selesai
                            if progress.completed > 0 and time.time() - last_activity > 10:
                                logger.info(f"[Grok] Timeout, sudah terkumpul {progress.completed} gambar")
                                break
                            continue

                    # Simpan gambar final
                    result_urls, result_b64 = await self._save_final_images(progress, n)

                    if result_urls:
                        return {
                            "success": True,
                            "urls": result_urls,
                            "b64_list": result_b64,
                            "count": len(result_urls)
                        }
                    elif error_info:
                        return {"success": False, **error_info}
                    else:
                        # Cek apakah blocked
                        if progress.check_blocked():
                            return {
                                "success": False,
                                "error_code": "blocked",
                                "error": "Generate diblokir, tidak dapat mendapatkan gambar final"
                            }
                        return {"success": False, "error": "Tidak menerima data gambar"}

        except aiohttp.ClientError as e:
            logger.error(f"[Grok] Error koneksi: {e}")
            return {"success": False, "error": f"Koneksi gagal: {e}"}

    async def _do_generate_video(
        self,
        sso: str,
        prompt: str,
        aspect_ratio: str,
        duration_seconds: int,
        resolution: str,
        preset: str,
        enable_nsfw: bool
    ) -> Dict[str, Any]:
        if CURL_CFFI_AVAILABLE:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._sync_do_generate_video_via_curl(
                        sso=sso,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio,
                        duration_seconds=duration_seconds,
                        resolution=resolution,
                        preset=preset,
                    )
                )

                if result.get("success"):
                    urls = result.get("urls", [])
                    if urls:
                        saved_url = await self._save_video_output(urls[0], "", sso=sso)
                        result["urls"] = [saved_url]
                return result
            except Exception as e:
                logger.warning(f"[Grok-Video] curl flow gagal, fallback aiohttp: {e}")

        connector = self._get_connector()

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                post_id = await self._create_video_post(session, sso, prompt)
                if not post_id:
                    return {
                        "success": False,
                        "error_code": "video_post_failed",
                        "error": "Gagal membuat video post"
                    }

                payload = self._build_video_chat_payload(
                    prompt=prompt,
                    post_id=post_id,
                    aspect_ratio=aspect_ratio,
                    duration_seconds=duration_seconds,
                    resolution=resolution,
                    preset=preset,
                )
                headers = self._get_http_headers(sso, referer="https://grok.com/")

                async with session.post(
                    self.APP_CHAT_NEW_URL,
                    json=payload,
                    headers=headers,
                    timeout=settings.GENERATION_TIMEOUT,
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        if response.status == 429:
                            return {
                                "success": False,
                                "error_code": "rate_limit_exceeded",
                                "error": "Rate limit exceeded"
                            }
                        if response.status == 401:
                            return {
                                "success": False,
                                "error_code": "unauthorized",
                                "error": "Unauthorized"
                            }
                        return {
                            "success": False,
                            "error": f"Video chat failed ({response.status}): {body[:300]}"
                        }

                    seen_types = set()
                    preview_image_urls: List[str] = []
                    final_video_url = ""
                    final_thumbnail_url = ""
                    buffer = ""

                    async for chunk in response.content.iter_chunked(4096):
                        text = chunk.decode("utf-8", errors="ignore")
                        buffer += text

                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            if not line or line == "[DONE]":
                                continue

                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            resp = data.get("result", {}).get("response", {})
                            if not isinstance(resp, dict):
                                continue

                            if resp.get("token"):
                                seen_types.add("token")

                            video_resp = resp.get("streamingVideoGenerationResponse", {})
                            if isinstance(video_resp, dict) and video_resp:
                                seen_types.add("streamingVideoGenerationResponse")
                                progress = int(video_resp.get("progress", 0) or 0)
                                video_url = video_resp.get("videoUrl", "")
                                thumb_url = video_resp.get("thumbnailImageUrl", "")

                                if thumb_url and thumb_url not in preview_image_urls:
                                    preview_image_urls.append(thumb_url)

                                if progress >= 100 and video_url:
                                    final_video_url = video_url
                                    final_thumbnail_url = thumb_url
                                    break

                        if final_video_url:
                            break

                    if final_video_url:
                        final_video_url = await self._upscale_video_url(
                            session=session,
                            sso=sso,
                            video_url=final_video_url,
                            resolution=resolution,
                        )

                        saved_url = await self._save_video_output(final_video_url, "", sso=sso)
                        result: Dict[str, Any] = {
                            "success": True,
                            "urls": [saved_url],
                            "count": 1,
                            "seen_types": sorted(list(seen_types)),
                        }
                        if final_thumbnail_url:
                            result["thumbnail_url"] = final_thumbnail_url
                        return result

                    return {
                        "success": False,
                        "error_code": "video_not_supported",
                        "error": "Video progress/event tidak ditemukan dari app-chat stream",
                        "seen_types": sorted(list(seen_types)),
                        "image_preview_urls": preview_image_urls[:3],
                    }

        except aiohttp.ClientError as e:
            logger.error(f"[Grok-Video] Error koneksi: {e}")
            return {"success": False, "error": f"Koneksi gagal: {e}"}

    async def _save_video_output(self, source_url: str, blob: str, sso: Optional[str] = None) -> str:
        settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

        normalized_url = source_url
        if normalized_url and not normalized_url.startswith("http://") and not normalized_url.startswith("https://"):
            normalized_url = f"https://assets.grok.com/{normalized_url.lstrip('/')}"

        if blob:
            ext = "mp4"
            if normalized_url and "." in normalized_url.rsplit("/", 1)[-1]:
                ext_candidate = normalized_url.rsplit("/", 1)[-1].split(".")[-1].lower()
                if ext_candidate in ["mp4", "webm", "mov"]:
                    ext = ext_candidate

            filename = f"{uuid.uuid4()}.{ext}"
            filepath = settings.VIDEOS_DIR / filename

            video_data = base64.b64decode(blob)
            with open(filepath, "wb") as file:
                file.write(video_data)

            local_url = f"{settings.get_base_url()}/videos/{filename}"
            logger.info(f"[Grok-Video] Simpan video: {filename} ({len(video_data) / 1024:.1f}KB)")
            return local_url

        if normalized_url:
            ext = "mp4"
            if "." in normalized_url.rsplit("/", 1)[-1]:
                ext_candidate = normalized_url.rsplit("/", 1)[-1].split(".")[-1].lower()
                if ext_candidate in ["mp4", "webm", "mov"]:
                    ext = ext_candidate

            filename = f"{uuid.uuid4()}.{ext}"
            filepath = settings.VIDEOS_DIR / filename

            try:
                connector = self._get_connector()
                async with aiohttp.ClientSession(connector=connector) as session:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Referer": "https://grok.com/",
                    }
                    if sso:
                        cookie = f"sso={sso}; sso-rw={sso}"
                        if settings.CF_CLEARANCE:
                            cookie += f"; cf_clearance={settings.CF_CLEARANCE}"
                        headers["Cookie"] = cookie

                    async with session.get(normalized_url, headers=headers, timeout=settings.GENERATION_TIMEOUT) as response:
                        if response.status == 200:
                            data = await response.read()
                            if data:
                                with open(filepath, "wb") as file:
                                    file.write(data)
                                local_url = f"{settings.get_base_url()}/videos/{filename}"
                                logger.info(f"[Grok-Video] Download video ke lokal: {filename} ({len(data) / 1024:.1f}KB)")
                                return local_url
                        logger.warning(f"[Grok-Video] Download video gagal: status={response.status}")
            except Exception as e:
                logger.warning(f"[Grok-Video] Download video error: {e}")

            if CURL_CFFI_AVAILABLE:
                try:
                    loop = asyncio.get_event_loop()
                    proxy = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY

                    def _curl_download() -> bytes:
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                            "Accept": "*/*",
                            "Referer": "https://grok.com/",
                        }
                        if sso:
                            cookie = f"sso={sso}; sso-rw={sso}"
                            if settings.CF_CLEARANCE:
                                cookie += f"; cf_clearance={settings.CF_CLEARANCE}"
                            headers["Cookie"] = cookie

                        resp = curl_requests.get(
                            normalized_url,
                            headers=headers,
                            impersonate="chrome133a",
                            proxy=proxy,
                            verify=False,
                            timeout=settings.GENERATION_TIMEOUT,
                        )
                        if resp.status_code == 200:
                            return resp.content
                        return b""

                    data = await loop.run_in_executor(None, _curl_download)
                    if data:
                        with open(filepath, "wb") as file:
                            file.write(data)
                        local_url = f"{settings.get_base_url()}/videos/{filename}"
                        logger.info(f"[Grok-Video] Download video via curl ke lokal: {filename} ({len(data) / 1024:.1f}KB)")
                        return local_url
                except Exception as e:
                    logger.warning(f"[Grok-Video] Download video via curl error: {e}")

        return normalized_url

    async def _save_final_images(
        self,
        progress: GenerationProgress,
        n: int
    ) -> tuple[List[str], List[str]]:
        """Simpan gambar final ke lokal, sekaligus kembalikan daftar URL dan daftar base64"""
        result_urls = []
        result_b64 = []
        settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        # Prioritas simpan versi final, jika tidak ada gunakan versi terbesar
        saved_ids = set()

        for img in sorted(
            progress.images.values(),
            key=lambda x: (x.is_final, x.blob_size),
            reverse=True
        ):
            if img.image_id in saved_ids:
                continue
            if len(saved_ids) >= n:
                break

            try:
                image_data = base64.b64decode(img.blob)

                # Tentukan ekstensi berdasarkan apakah versi final
                ext = "jpg" if img.is_final else "png"
                filename = f"{img.image_id}.{ext}"
                filepath = settings.IMAGES_DIR / filename

                with open(filepath, 'wb') as f:
                    f.write(image_data)

                url = f"{settings.get_base_url()}/images/{filename}"
                result_urls.append(url)
                result_b64.append(img.blob)
                saved_ids.add(img.image_id)

                logger.info(
                    f"[Grok] Simpan gambar: {filename} "
                    f"({len(image_data) / 1024:.1f}KB, {img.stage})"
                )

            except Exception as e:
                logger.error(f"[Grok] Gagal menyimpan gambar: {e}")

        return result_urls, result_b64

    async def generate_stream(
        self,
        prompt: str,
        aspect_ratio: str = "2:3",
        n: int = None,
        enable_nsfw: bool = True,
        sso: Optional[str] = None
    ):
        """
        Generate gambar secara streaming - Menggunakan async generator

        Yields:
            Dict berisi informasi progress gambar saat ini
        """        
        # Gunakan jumlah gambar default dari konfigurasi
        if n is None:
            n = settings.DEFAULT_IMAGE_COUNT

        queue: asyncio.Queue = asyncio.Queue()
        done = asyncio.Event()

        async def callback(img: ImageProgress, prog: GenerationProgress):
            await queue.put({
                "type": "progress",
                "image_id": img.image_id,
                "stage": img.stage,
                "blob_size": img.blob_size,
                "is_final": img.is_final,
                "completed": prog.completed,
                "total": prog.total
            })

        async def generate_task():
            result = await self.generate(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n=n,
                enable_nsfw=enable_nsfw,
                sso=sso,
                stream_callback=callback
            )
            await queue.put({"type": "result", **result})
            done.set()

        task = asyncio.create_task(generate_task())

        try:
            while not done.is_set() or not queue.empty():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield item
                    if item.get("type") == "result":
                        break
                except asyncio.TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()


# Instance global
grok_client = GrokImagineClient()
