"""Imagine API Routes - Format kompatibel OpenAI, mendukung preview streaming"""

import time
import json
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.security import require_api_key
from app.core.logger import logger
from app.services.grok_client import grok_client


router = APIRouter()


# ============== Model Request/Response ==============

class OpenAIImageRequest(BaseModel):
    """Request pembuatan gambar yang kompatibel dengan OpenAI"""
    prompt: str = Field(..., description="Prompt deskripsi gambar", min_length=1)
    model: Optional[str] = Field("grok-2-image", description="Nama model")
    n: Optional[int] = Field(None, description="Jumlah yang dihasilkan, jika tidak ditentukan gunakan konfigurasi default", ge=1, le=4)
    size: Optional[str] = Field("1024x1536", description="Ukuran gambar")
    aspect_ratio: Optional[str] = Field(None, description="Rasio aspek langsung (1:1, 2:3, 3:2, 9:16, 16:9)")
    response_format: Optional[str] = Field("url", description="Format response: url atau b64_json")
    stream: Optional[bool] = Field(False, description="Apakah mengembalikan progress secara streaming")

    class Config:
        json_schema_extra = {
            "example": {
                "prompt": "a beautiful sunset over the ocean",
                "n": 2,
                "size": "1024x1536"
            }
        }


class OpenAIImageData(BaseModel):
    """Data gambar dengan format OpenAI"""
    url: Optional[str] = None
    b64_json: Optional[str] = None


class OpenAIImageResponse(BaseModel):
    """Response gambar yang kompatibel dengan OpenAI"""
    created: int
    data: List[OpenAIImageData]


class OpenAIVideoRequest(BaseModel):
    """Request pembuatan video (eksperimental)"""
    prompt: str = Field(..., description="Prompt deskripsi video", min_length=1)
    model: Optional[str] = Field("grok-2-video", description="Nama model")
    size: Optional[str] = Field("1792x1024", description="Ukuran video")
    aspect_ratio: Optional[str] = Field(None, description="Rasio aspek langsung (1:1, 2:3, 3:2, 9:16, 16:9)")
    duration_seconds: Optional[int] = Field(6, description="Durasi video (6 atau 10 detik)")
    resolution: Optional[str] = Field("480p", description="Resolusi video (480p atau 720p)")
    preset: Optional[str] = Field("normal", description="Preset video (fun, normal, spicy, custom)")
    response_format: Optional[str] = Field("url", description="Format response: url atau b64_json")


class OpenAIVideoData(BaseModel):
    """Data video"""
    url: Optional[str] = None
    b64_json: Optional[str] = None


class OpenAIVideoResponse(BaseModel):
    """Response video"""
    created: int
    data: List[OpenAIVideoData]


# ============== Fungsi Helper ==============


def size_to_aspect_ratio(size: str) -> str:
    """Konversi size OpenAI ke aspect_ratio"""
    size_map = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "1024x1792": "9:16",
        "1792x1024": "16:9",
        "512x512": "1:1",
        "256x256": "1:1",
    }
    return size_map.get(size, "2:3")


ALLOWED_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "9:16", "16:9"}


def resolve_aspect_ratio(request: OpenAIImageRequest) -> str:
    """Menentukan aspect ratio final dari request"""
    if request.aspect_ratio:
        if request.aspect_ratio not in ALLOWED_ASPECT_RATIOS:
            raise HTTPException(
                status_code=400,
                detail="Invalid aspect_ratio. Gunakan salah satu: 1:1, 2:3, 3:2, 9:16, 16:9"
            )
        return request.aspect_ratio

    return size_to_aspect_ratio(request.size)


def resolve_video_aspect_ratio(request: OpenAIVideoRequest) -> str:
    """Menentukan aspect ratio final untuk request video"""
    if request.aspect_ratio:
        if request.aspect_ratio not in ALLOWED_ASPECT_RATIOS:
            raise HTTPException(
                status_code=400,
                detail="Invalid aspect_ratio. Gunakan salah satu: 1:1, 2:3, 3:2, 9:16, 16:9"
            )
        return request.aspect_ratio

    return size_to_aspect_ratio(request.size)


def validate_video_options(request: OpenAIVideoRequest):
    """Validasi opsi video berdasarkan opsi UI Grok saat ini"""
    if request.duration_seconds not in [6, 10]:
        raise HTTPException(status_code=400, detail="Invalid duration_seconds. Gunakan 6 atau 10")

    if request.resolution not in ["480p", "720p"]:
        raise HTTPException(status_code=400, detail="Invalid resolution. Gunakan 480p atau 720p")

    if request.preset not in ["fun", "normal", "spicy", "custom"]:
        raise HTTPException(status_code=400, detail="Invalid preset. Gunakan fun, normal, spicy, atau custom")


# ============== API Routes ==============

@router.post("/images/generations", response_model=OpenAIImageResponse)
async def generate_image(
    request: OpenAIImageRequest,
    _: bool = Depends(require_api_key)
):
    """
    Generate gambar (API kompatibel OpenAI)

    Mendukung dua mode:
    - stream=false (default): Mengembalikan hasil lengkap
    - stream=true: Mengembalikan progress generate secara streaming (format SSE)
    """
    logger.info(f"[API] Request generate: {request.prompt[:50]}... stream={request.stream}")

    aspect_ratio = resolve_aspect_ratio(request)

    # Mode streaming
    if request.stream:
        return StreamingResponse(
            stream_generate(
                prompt=request.prompt,
                aspect_ratio=aspect_ratio,
                n=request.n
            ),
            media_type="text/event-stream"
        )

    # Mode normal
    result = await grok_client.generate(
        prompt=request.prompt,
        aspect_ratio=aspect_ratio,
        n=request.n,
        enable_nsfw=True
    )

    if not result.get("success"):
        error_msg = result.get("error", "Image generation failed")
        error_code = result.get("error_code", "")

        if error_code == "rate_limit_exceeded":
            raise HTTPException(status_code=429, detail=error_msg)
        else:
            raise HTTPException(status_code=500, detail=error_msg)

    # Kembalikan sesuai response_format secara ketat
    if request.response_format == "b64_json":
        # Kembalikan format base64
        b64_list = result.get("b64_list", [])
        data = [OpenAIImageData(b64_json=b64) for b64 in b64_list]
    else:
        # Kembalikan format URL
        data = [OpenAIImageData(url=url) for url in result.get("urls", [])]

    return OpenAIImageResponse(
        created=int(time.time()),
        data=data
    )


async def stream_generate(prompt: str, aspect_ratio: str, n: int):
    """
    Generate gambar secara streaming

    Output format SSE:
    - event: progress - Update progress generate
    - event: complete - Generate selesai, berisi URL akhir
    - event: error - Terjadi error
    """
    try:
        async for item in grok_client.generate_stream(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            n=n,
            enable_nsfw=True
        ):
            if item.get("type") == "progress":
                # Update progress
                event_data = {
                    "image_id": item["image_id"],
                    "stage": item["stage"],
                    "is_final": item["is_final"],
                    "completed": item["completed"],
                    "total": item["total"],
                    "progress": f"{item['completed']}/{item['total']}"
                }
                yield f"event: progress\ndata: {json.dumps(event_data)}\n\n"

            elif item.get("type") == "result":
                # Hasil akhir
                if item.get("success"):
                    result_data = {
                        "created": int(time.time()),
                        "data": [{"url": url} for url in item.get("urls", [])]
                    }
                    yield f"event: complete\ndata: {json.dumps(result_data)}\n\n"
                else:
                    error_data = {"error": item.get("error", "Generation failed")}
                    yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
                break

    except Exception as e:
        logger.error(f"[API] Error generate streaming: {e}")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


@router.get("/models/imagine")
async def list_imagine_models():
    """Menampilkan daftar model pembuatan gambar"""
    return {
        "object": "list",
        "data": [
            {
                "id": "grok-imagine",
                "object": "model",
                "created": 1700000000,
                "owned_by": "xai"
            }
        ]
    }


@router.post("/videos/generations", response_model=OpenAIVideoResponse)
async def generate_video(
    request: OpenAIVideoRequest,
    _: bool = Depends(require_api_key)
):
    """Generate video (eksperimental)"""
    logger.info(f"[API] Request video: {request.prompt[:50]}...")

    validate_video_options(request)
    aspect_ratio = resolve_video_aspect_ratio(request)

    result = await grok_client.generate_video(
        prompt=request.prompt,
        aspect_ratio=aspect_ratio,
        duration_seconds=request.duration_seconds,
        resolution=request.resolution,
        preset=request.preset,
        enable_nsfw=True
    )

    if not result.get("success"):
        error_msg = result.get("error", "Video generation failed")
        error_code = result.get("error_code", "")

        if error_code == "rate_limit_exceeded":
            raise HTTPException(status_code=429, detail=error_msg)
        if error_code == "video_not_supported":
            raise HTTPException(
                status_code=501,
                detail={
                    "error": error_msg,
                    "seen_types": result.get("seen_types", []),
                    "image_preview_urls": result.get("image_preview_urls", [])
                }
            )
        raise HTTPException(status_code=500, detail=error_msg)

    if request.response_format == "b64_json":
        data = [OpenAIVideoData(b64_json=b64) for b64 in result.get("b64_list", [])]
    else:
        data = [OpenAIVideoData(url=url) for url in result.get("urls", [])]

    return OpenAIVideoResponse(
        created=int(time.time()),
        data=data
    )
