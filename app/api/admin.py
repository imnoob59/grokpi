"""Admin API Routes"""

import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException
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

router = APIRouter()


@router.get("/status")
async def get_status():
    """Mendapatkan status service"""
    # Versi Redis bersifat asinkron
    if hasattr(sso_manager, 'get_status') and asyncio.iscoroutinefunction(sso_manager.get_status):
        sso_status = await sso_manager.get_status()
    else:
        sso_status = sso_manager.get_status()

    # Membangun informasi konfigurasi proxy
    proxy_config = {
        "proxy_url": settings.PROXY_URL,
        "http_proxy": settings.HTTP_PROXY,
        "https_proxy": settings.HTTPS_PROXY
    }
    # Filter nilai None
    proxy_config = {k: v for k, v in proxy_config.items() if v}

    return {
        "service": "running",
        "sso": sso_status,
        "proxy": proxy_config if proxy_config else "none",
        "config": {
            "host": settings.HOST,
            "port": settings.PORT,
            "images_dir": str(settings.IMAGES_DIR),
            "videos_dir": str(settings.VIDEOS_DIR),
            "base_url": settings.get_base_url(),
            "sso_file": str(settings.SSO_FILE),
            "redis_enabled": settings.REDIS_ENABLED,
            "rotation_strategy": settings.SSO_ROTATION_STRATEGY,
            "daily_limit": settings.SSO_DAILY_LIMIT
        }
    }


@router.post("/sso/reload")
async def reload_sso():
    """Muat ulang daftar SSO"""
    count = await sso_manager.reload()
    logger.info(f"[Admin] Muat ulang SSO: {count} keys")
    return {
        "success": True,
        "count": count
    }


@router.post("/sso/reset-usage")
async def reset_sso_usage():
    """Reset manual jumlah penggunaan harian (hanya mode Redis)"""
    if hasattr(sso_manager, 'reset_daily_usage'):
        await sso_manager.reset_daily_usage()
        logger.info("[Admin] Reset manual jumlah penggunaan harian")
        return {"success": True, "message": "Jumlah penggunaan harian telah direset"}
    return {"success": False, "message": "Fitur ini hanya tersedia dalam mode Redis"}


@router.get("/images/list")
async def list_images(limit: int = 50):
    """Menampilkan daftar gambar yang di-cache"""
    images = []
    if settings.IMAGES_DIR.exists():
        files = sorted(settings.IMAGES_DIR.glob("*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files[:limit]:
            images.append({
                "filename": f.name,
                "url": f"{settings.get_base_url()}/images/{f.name}",
                "size": f.stat().st_size
            })
    return {"images": images, "count": len(images)}


@router.delete("/images/clear")
async def clear_images():
    """Menghapus cache gambar"""
    count = 0
    if settings.IMAGES_DIR.exists():
        for f in settings.IMAGES_DIR.glob("*"):
            if f.is_file():
                f.unlink()
                count += 1

    logger.info(f"[Admin] Telah menghapus {count} gambar")
    return {"success": True, "deleted": count}


@router.delete("/media/image/{filename}")
async def delete_image(filename: str):
    """Hapus satu file gambar"""
    safe_name = Path(filename).name
    target = settings.IMAGES_DIR / safe_name

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File gambar tidak ditemukan")

    target.unlink()
    logger.info(f"[Admin] Hapus gambar: {safe_name}")
    return {"success": True, "deleted": safe_name}


@router.delete("/media/video/{filename}")
async def delete_video(filename: str):
    """Hapus satu file video"""
    safe_name = Path(filename).name
    target = settings.VIDEOS_DIR / safe_name

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File video tidak ditemukan")

    target.unlink()
    logger.info(f"[Admin] Hapus video: {safe_name}")
    return {"success": True, "deleted": safe_name}
