"""Chat Completions API - Gateway LLM yang kompatibel dengan OpenAI, untuk pembuatan gambar"""

import time
import json
import uuid
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.security import require_api_key
from app.core.logger import logger
from app.services.grok_client import grok_client, ImageProgress, GenerationProgress


router = APIRouter()


# ============== Model Request/Response ==============

class ChatMessage(BaseModel):
    """Pesan chat"""
    role: str = Field(..., description="Peran: user/assistant/system")
    content: str = Field(..., description="Konten pesan")


class ChatCompletionRequest(BaseModel):
    """Request OpenAI Chat Completion"""
    model: str = Field("grok-imagine", description="Nama model")
    messages: List[ChatMessage] = Field(..., description="Daftar pesan")
    stream: bool = Field(True, description="Apakah mengembalikan stream")
    max_tokens: Optional[int] = Field(4096, description="Jumlah token maksimal")
    temperature: Optional[float] = Field(1.0, description="Temperature")
    n: Optional[int] = Field(4, description="Jumlah gambar yang dihasilkan", ge=1, le=4)

    class Config:
        json_schema_extra = {
            "example": {
                "model": "grok-imagine",
                "messages": [{"role": "user", "content": "Gambar seekor kucing yang lucu"}],
                "stream": True
            }
        }


# ============== Fungsi Helper ==============


def extract_prompt(messages: List[ChatMessage]) -> str:
    """Ekstrak prompt pembuatan gambar dari daftar pesan"""
    # Ambil pesan user terakhir sebagai prompt
    for msg in reversed(messages):
        if msg.role == "user" and msg.content.strip():
            return msg.content.strip()
    return ""


def create_chat_chunk(
    chunk_id: str,
    content: str = "",
    finish_reason: Optional[str] = None,
    thinking: Optional[str] = None,
    thinking_progress: Optional[int] = None
) -> str:
    """Membuat blok response chat dengan format SSE"""
    delta: Dict[str, Any] = {}

    if content:
        delta["content"] = content
    if thinking:
        delta["thinking"] = thinking
    if thinking_progress is not None:
        delta["thinking_progress"] = thinking_progress

    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "grok-imagine",
        "choices": [{
            "index": 0,
            "delta": delta if delta else {},
            "finish_reason": finish_reason
        }]
    }

    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ============== API Routes ==============

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    _: bool = Depends(require_api_key)
):
    """
    Chat Completions API yang kompatibel dengan OpenAI

    User memasukkan konten yang ingin digambar, mengembalikan progress thinking secara stream dan URL gambar akhir
    """
    # Ekstrak prompt
    prompt = extract_prompt(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="No prompt found in messages")

    logger.info(f"[Chat] Request generate: {prompt[:50]}... n={request.n}")

    # Mode streaming
    if request.stream:
        return StreamingResponse(
            stream_chat_generate(prompt=prompt, n=request.n),
            media_type="text/event-stream"
        )

    # Mode non-streaming - tunggu sampai selesai lalu return
    result = await grok_client.generate(
        prompt=prompt,
        n=request.n,
        enable_nsfw=True
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Image generation failed")
        )

    # Membangun konten response
    urls = result.get("urls", [])
    content = "Gambar telah dihasilkan untuk Anda:\n\n" + "\n".join([f"![Gambar]({url})" for url in urls])

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "grok-imagine",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": len(prompt),
            "completion_tokens": len(content),
            "total_tokens": len(prompt) + len(content)
        }
    }


async def stream_chat_generate(prompt: str, n: int):
    """
    Generate gambar secara streaming, output progress thinking dan URL akhir

    Pemetaan progress:
    - preview: 33%
    - medium: 66%
    - final: 99%
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    # Pemetaan tahap ke progress
    stage_progress = {
        "preview": 33,
        "medium": 66,
        "final": 99
    }

    # Catat tahap terbaru untuk setiap gambar, hindari output duplikat
    image_stages: Dict[str, str] = {}
    final_urls: List[str] = []

    try:
        # Mulai thinking
        yield create_chat_chunk(
            chunk_id,
            thinking=f"Sedang membuat gambar untuk Anda: {prompt[:50]}...",
            thinking_progress=0
        )

        async for item in grok_client.generate_stream(
            prompt=prompt,
            n=n,
            enable_nsfw=True
        ):
            if item.get("type") == "progress":
                image_id = item["image_id"]
                stage = item["stage"]
                completed = item["completed"]
                total = item["total"]

                # Hanya output saat tahap berubah
                if image_stages.get(image_id) != stage:
                    image_stages[image_id] = stage
                    progress = stage_progress.get(stage, 0)

                    # Hitung overall progress
                    overall_progress = int((completed / total) * 100) if total > 0 else progress

                    # Membangun konten thinking
                    stage_names = {"preview": "Preview", "medium": "Medium", "final": "HD"}
                    thinking_text = (
                        f"Gambar {len(image_stages)}/{total} - "
                        f"{stage_names.get(stage, stage)} ({progress}%)"
                    )

                    yield create_chat_chunk(
                        chunk_id,
                        thinking=thinking_text,
                        thinking_progress=progress
                    )

            elif item.get("type") == "result":
                if item.get("success"):
                    final_urls = item.get("urls", [])

                    # Output 100% selesai
                    yield create_chat_chunk(
                        chunk_id,
                        thinking=f"Generate selesai! Total {len(final_urls)} gambar",
                        thinking_progress=100
                    )

                    # Output konten akhir - menggunakan format gambar Markdown
                    content = "Gambar telah dihasilkan untuk Anda:\n\n"
                    for i, url in enumerate(final_urls, 1):
                        content += f"![Gambar{i}]({url})\n\n"

                    yield create_chat_chunk(chunk_id, content=content)

                else:
                    # Error
                    error_msg = item.get("error", "Generate gagal")
                    yield create_chat_chunk(
                        chunk_id,
                        content=f"Generate gagal: {error_msg}"
                    )

                # Selesai
                yield create_chat_chunk(chunk_id, finish_reason="stop")
                break

        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"[Chat] Error generate streaming: {e}")
        yield create_chat_chunk(chunk_id, content=f"Error generate: {str(e)}")
        yield create_chat_chunk(chunk_id, finish_reason="stop")
        yield "data: [DONE]\n\n"


@router.get("/models")
async def list_models():
    """Menampilkan daftar model yang tersedia"""
    return {
        "object": "list",
        "data": [
            {
                "id": "grok-imagine",
                "object": "model",
                "created": 1700000000,
                "owned_by": "xai",
                "permission": [],
                "root": "grok-imagine",
                "parent": None
            }
        ]
    }
