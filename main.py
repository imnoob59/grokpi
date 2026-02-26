"""Grok Imagine API Gateway

Gateway API proxy untuk pembuatan gambar Grok, membungkus Grok Imagine sebagai REST API yang kompatibel dengan OpenAI.
Menggunakan koneksi langsung WebSocket ke Grok, tanpa memerlukan otomatisasi browser, meminimalkan penggunaan resource.
"""

import time
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.imagine import router as imagine_router
from app.api.chat import router as chat_router
from app.api.admin import router as admin_router
from app.core.config import settings
from app.core.logger import logger, get_uvicorn_log_config
from app.services.sso_manager import sso_manager


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware logging request"""
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        logger.info(f"[Request] {request.method} {request.url.path}")

        response = await call_next(request)

        duration = time.time() - start_time
        logger.info(f"[Response] {request.method} {request.url.path} -> {response.status_code} ({duration:.2f}s)")
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manajemen lifecycle aplikasi"""
    # Inisialisasi ulang log di subprocess
    from app.core.logger import setup_logger
    setup_logger()

    logger.info("=" * 50)
    logger.info("Grok Imagine API Gateway sedang memulai...")

    # Tampilkan informasi konfigurasi
    logger.info(f"[Config] HOST: {settings.HOST}")
    logger.info(f"[Config] PORT: {settings.PORT}")
    logger.info(f"[Config] BASE_URL: {settings.get_base_url()}")

    # Konfigurasi proxy
    if settings.PROXY_URL:
        logger.info(f"[Config] PROXY_URL: {settings.PROXY_URL}")
    elif settings.HTTP_PROXY or settings.HTTPS_PROXY:
        logger.info(f"[Config] HTTP_PROXY: {settings.HTTP_PROXY}")
        logger.info(f"[Config] HTTPS_PROXY: {settings.HTTPS_PROXY}")
    else:
        logger.info("[Config] Proxy tidak dikonfigurasi")

    # Muat SSO
    logger.info(f"[SSO] Memuat dari file: {settings.SSO_FILE}")
    if hasattr(sso_manager, "load_sso_list"):
        count = sso_manager.load_sso_list()
    elif hasattr(sso_manager, "initialize") and asyncio.iscoroutinefunction(sso_manager.initialize):
        count = await sso_manager.initialize()
    else:
        count = 0
        logger.warning("[SSO] Manager tidak memiliki metode load/inisialisasi yang dikenali")
    logger.info(f"[SSO] Telah memuat {count} SSO")

    # Pastikan direktori media ada
    settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    yield

    logger.info("Grok Imagine API Gateway telah ditutup")


app = FastAPI(
    title="Grok Imagine API Gateway",
    description=(
        "Gateway API kompatibel OpenAI untuk **image** dan **video generation** menggunakan Grok.\n\n"
        "### Fitur Utama\n"
        "- OpenAI-style endpoint untuk image, video, dan chat\n"
        "- Streaming progress untuk image\n"
        "- Auto download hasil media ke cache lokal\n"
        "- Gallery modern untuk image dan video\n"
        "- Multi-SSO rotation + fallback retry\n\n"
        "### Endpoint Publik\n"
        "- `POST /v1/images/generations`\n"
        "- `POST /v1/videos/generations`\n"
        "- `POST /v1/chat/completions`\n"
        "- `GET /gallery`\n"
        "- `GET /video-gallery`\n"
    ),
    version="2.0.0",
    openapi_tags=[
        {"name": "Chat", "description": "Endpoint chat-kompatibel OpenAI untuk workflow generate media."},
        {"name": "Images", "description": "Endpoint pembuatan gambar dan video kompatibel OpenAI."},
        {"name": "Admin", "description": "Endpoint administrasi, status, dan manajemen cache media."},
    ],
    swagger_ui_parameters={
        "docExpansion": "list",
        "defaultModelsExpandDepth": -1,
        "displayRequestDuration": True,
        "persistAuthorization": True,
        "tryItOutEnabled": True,
    },
    lifespan=lifespan
)

# Middleware logging request (letakkan di paling depan)
app.add_middleware(RequestLoggingMiddleware)

# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pastikan direktori media ada
settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
settings.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

# Service file statis (cache gambar)
app.mount("/images", StaticFiles(directory=str(settings.IMAGES_DIR)), name="images")
app.mount("/videos", StaticFiles(directory=str(settings.VIDEOS_DIR)), name="videos")

# Daftarkan routes
app.include_router(chat_router, prefix="/v1", tags=["Chat"])
app.include_router(imagine_router, prefix="/v1", tags=["Images"])
app.include_router(admin_router, prefix="/admin", tags=["Admin"])


@app.get("/")
async def root():
    """Informasi service"""
    return {
        "service": "Grok Imagine API Gateway",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health")
async def health():
    """Health check"""
    if hasattr(sso_manager, 'get_status') and asyncio.iscoroutinefunction(sso_manager.get_status):
        sso_status = await sso_manager.get_status()
    else:
        sso_status = sso_manager.get_status()

    total = sso_status.get("total", sso_status.get("total_keys", 0))
    failed = sso_status.get("failed", sso_status.get("failed_count", 0))

    return {
        "status": "healthy",
        "sso_count": total,
        "sso_failed": failed
    }


@app.get("/gallery", response_class=HTMLResponse)
async def gallery():
    """Galeri gambar - Lihat gambar yang dihasilkan secara real-time"""
    from datetime import datetime

    images = []
    if settings.IMAGES_DIR.exists():
        for f in settings.IMAGES_DIR.iterdir():
            if f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                stat = f.stat()
                images.append({
                    "name": f.name,
                    "url": f"/images/{f.name}",
                    "mtime": stat.st_mtime,
                    "size": stat.st_size
                })

    # Urutkan berdasarkan waktu modifikasi terbalik
    images.sort(key=lambda x: x["mtime"], reverse=True)

    image_cards = ""
    for img in images[:60]:
        dt = datetime.fromtimestamp(img["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        size_kb = img["size"] / 1024
        image_cards += f'''
        <article class="card" data-name="{img['name']}">
            <a href="{img['url']}" target="_blank" class="preview-link">
                <img src="{img['url']}" alt="{img['name']}" loading="lazy">
            </a>
            <div class="info">
                <span class="name">{img['name']}</span>
                <span class="meta">{dt} • {size_kb:.1f} KB</span>
            </div>
            <div class="actions">
                <a class="btn btn-ghost" href="{img['url']}" target="_blank">Open</a>
                <button class="btn btn-danger" onclick="deleteImage('{img['name']}')">Delete</button>
            </div>
        </article>
        '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Grok Media - Image Gallery</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: radial-gradient(circle at top, #1f2844 0%, #111628 55%, #0d1220 100%);
                color: #e8ebf3;
                min-height: 100vh;
                padding: 20px;
            }}
            h1 {{
                text-align: center;
                margin-bottom: 8px;
                background: linear-gradient(135deg, #7f9cff 0%, #8c6dff 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .subtitle {{ text-align: center; color: #98a2b3; margin-bottom: 18px; }}
            .toolbar {{
                display: flex;
                justify-content: center;
                gap: 10px;
                margin-bottom: 18px;
            }}
            .btn {{
                border: 1px solid rgba(255,255,255,0.14);
                color: #f5f7ff;
                background: rgba(255,255,255,0.06);
                border-radius: 10px;
                padding: 8px 14px;
                font-size: 12px;
                text-decoration: none;
                cursor: pointer;
            }}
            .btn:hover {{ background: rgba(255,255,255,0.12); }}
            .btn-danger {{ border-color: rgba(255, 94, 125, 0.55); color: #ff9eb1; }}
            .btn-danger:hover {{ background: rgba(255, 94, 125, 0.16); }}
            .btn-ghost {{ color: #a7d3ff; border-color: rgba(122, 187, 255, 0.45); }}
            .gallery {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
                gap: 16px;
                max-width: 1400px;
                margin: 0 auto;
            }}
            .card {{
                background: rgba(15, 23, 42, 0.75);
                border: 1px solid rgba(255,255,255,0.08);
                backdrop-filter: blur(6px);
                border-radius: 12px;
                overflow: hidden;
                transition: transform 0.2s, box-shadow 0.2s;
            }}
            .card:hover {{
                transform: translateY(-3px);
                box-shadow: 0 12px 34px rgba(0,0,0,0.28);
            }}
            .card img {{
                width: 100%;
                height: 270px;
                object-fit: cover;
                display: block;
            }}
            .info {{
                padding: 10px 12px 4px;
            }}
            .name {{
                display: block;
                font-size: 12px;
                color: #d9def0;
                margin-bottom: 4px;
                word-break: break-all;
            }}
            .meta {{ font-size: 11px; color: #8a93a7; }}
            .actions {{ display: flex; gap: 8px; padding: 10px 12px 12px; }}
            .empty {{
                text-align: center;
                padding: 60px;
                color: #666;
            }}
            .toast {{
                position: fixed;
                right: 16px;
                bottom: 16px;
                background: rgba(9, 15, 30, 0.92);
                border: 1px solid rgba(255,255,255,0.15);
                color: #eaf0ff;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 12px;
                display: none;
            }}
        </style>
    </head>
    <body>
        <h1>Image Gallery</h1>
        <p class="subtitle">Total {len(images)} gambar</p>
        <div class="toolbar">
            <button class="btn" onclick="location.reload()">Refresh</button>
            <a class="btn" href="/video-gallery">Video Gallery</a>
        </div>
        <div class="gallery">
            {image_cards if image_cards else '<div class="empty">Belum ada gambar</div>'}
        </div>
        <div id="toast" class="toast"></div>
        <script>
            function showToast(text) {{
                const toast = document.getElementById('toast');
                toast.textContent = text;
                toast.style.display = 'block';
                setTimeout(() => toast.style.display = 'none', 1800);
            }}

            async function deleteImage(filename) {{
                if (!confirm('Hapus image ini?')) return;
                const res = await fetch('/admin/media/image/' + encodeURIComponent(filename), {{ method: 'DELETE' }});
                if (res.ok) {{
                    const card = document.querySelector('[data-name="' + CSS.escape(filename) + '"]');
                    if (card) card.remove();
                    showToast('Image deleted');
                }} else {{
                    showToast('Gagal hapus image');
                }}
            }}
        </script>
    </body>
    </html>
    '''
    return html


@app.get("/video-gallery", response_class=HTMLResponse)
async def video_gallery():
    """Galeri video - Lihat video yang dihasilkan secara real-time"""
    from datetime import datetime

    videos = []
    if settings.VIDEOS_DIR.exists():
        for file in settings.VIDEOS_DIR.iterdir():
            if file.suffix.lower() in [".mp4", ".webm", ".mov", ".mkv"]:
                stat = file.stat()
                videos.append(
                    {
                        "name": file.name,
                        "url": f"/videos/{file.name}",
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    }
                )

    videos.sort(key=lambda x: x["mtime"], reverse=True)

    video_cards = ""
    for video in videos[:36]:
        dt = datetime.fromtimestamp(video["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        size_mb = video["size"] / (1024 * 1024)
        video_cards += f'''
        <article class="card" data-name="{video['name']}">
            <video controls preload="metadata" playsinline>
                <source src="{video['url']}" type="video/mp4">
            </video>
            <div class="info">
                <span class="name">{video['name']}</span>
                <span class="meta">{dt} • {size_mb:.2f} MB</span>
            </div>
            <div class="actions">
                <a class="btn btn-ghost" href="{video['url']}" target="_blank">Open</a>
                <button class="btn btn-danger" onclick="deleteVideo('{video['name']}')">Delete</button>
            </div>
        </article>
        '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Grok Media - Video Gallery</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: radial-gradient(circle at top, #162635 0%, #111827 55%, #0b1120 100%);
                color: #e8ebf3;
                min-height: 100vh;
                padding: 20px;
            }}
            h1 {{
                text-align: center;
                margin-bottom: 8px;
                background: linear-gradient(135deg, #3dd6ff 0%, #5a7dff 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .subtitle {{ text-align: center; color: #98a2b3; margin-bottom: 18px; }}
            .toolbar {{ display: flex; justify-content: center; gap: 10px; margin-bottom: 20px; }}
            .btn {{
                padding: 8px 14px;
                border: 1px solid rgba(255,255,255,0.14);
                border-radius: 10px;
                color: #f5f7ff;
                text-decoration: none;
                background: rgba(255,255,255,0.06);
                cursor: pointer;
                font-size: 12px;
            }}
            .btn:hover {{ background: rgba(255,255,255,0.12); }}
            .btn-danger {{ border-color: rgba(255, 94, 125, 0.55); color: #ff9eb1; }}
            .btn-danger:hover {{ background: rgba(255, 94, 125, 0.16); }}
            .btn-ghost {{ color: #a7d3ff; border-color: rgba(122, 187, 255, 0.45); }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
                gap: 16px;
                max-width: 1600px;
                margin: 0 auto;
            }}
            .card {{
                background: rgba(15, 23, 42, 0.75);
                border-radius: 12px;
                overflow: hidden;
                border: 1px solid rgba(255,255,255,0.08);
                backdrop-filter: blur(6px);
            }}
            .card video {{
                width: 100%;
                height: 230px;
                background: #000;
                display: block;
            }}
            .info {{ padding: 10px 12px 4px; }}
            .name {{ display: block; font-size: 12px; color: #ddd; word-break: break-all; margin-bottom: 4px; }}
            .meta {{ font-size: 11px; color: #8a93a7; }}
            .actions {{ display: flex; gap: 8px; padding: 10px 12px 12px; }}
            .empty {{ text-align: center; padding: 60px; color: #666; }}
            .toast {{
                position: fixed;
                right: 16px;
                bottom: 16px;
                background: rgba(9, 15, 30, 0.92);
                border: 1px solid rgba(255,255,255,0.15);
                color: #eaf0ff;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 12px;
                display: none;
            }}
        </style>
    </head>
    <body>
        <h1>Grok Video Gallery</h1>
        <p class="subtitle">Total {len(videos)} video</p>
        <div class="toolbar">
            <button class="btn" onclick="location.reload()">Refresh</button>
            <a class="btn" href="/gallery">Image Gallery</a>
        </div>
        <div class="grid">
            {video_cards if video_cards else '<div class="empty">Belum ada video</div>'}
        </div>
        <div id="toast" class="toast"></div>
        <script>
            function showToast(text) {{
                const toast = document.getElementById('toast');
                toast.textContent = text;
                toast.style.display = 'block';
                setTimeout(() => toast.style.display = 'none', 1800);
            }}

            async function deleteVideo(filename) {{
                if (!confirm('Hapus video ini?')) return;
                const res = await fetch('/admin/media/video/' + encodeURIComponent(filename), {{ method: 'DELETE' }});
                if (res.ok) {{
                    const card = document.querySelector('[data-name="' + CSS.escape(filename) + '"]');
                    if (card) card.remove();
                    showToast('Video deleted');
                }} else {{
                    showToast('Gagal hapus video');
                }}
            }}
        </script>
    </body>
    </html>
    '''
    return html


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_config=get_uvicorn_log_config()
    )
