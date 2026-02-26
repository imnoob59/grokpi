import asyncio
import argparse
import json
import time

import aiohttp

from app.core.config import settings


def load_sso() -> str | None:
    with open(settings.SSO_FILE, "r", encoding="utf-8") as file:
        for line in file:
            token = line.strip()
            if token and not token.startswith("#"):
                return token
    return None


async def run_case(name: str, extra_properties: dict):
    sso = load_sso()
    if not sso:
        print(f"[{name}] no sso token")
        return

    headers = {
        "Cookie": f"sso={sso}; sso-rw={sso}",
        "Origin": "https://grok.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    properties = {
        "section_count": 0,
        "is_kids_mode": False,
        "enable_nsfw": True,
        "skip_upsampler": False,
        "is_initial": False,
        "aspect_ratio": "16:9",
    }
    properties.update(extra_properties)

    payload = {
        "type": "conversation.item.create",
        "timestamp": int(time.time() * 1000),
        "item": {
            "type": "message",
            "content": [
                {
                    "requestId": f"probe-{name}-{int(time.time())}",
                    "text": "short cinematic city shot at night",
                    "type": "input_text",
                    "properties": properties,
                }
            ],
        },
    }

    print(f"\n=== CASE {name} ===")
    print(f"extra_properties={json.dumps(extra_properties, ensure_ascii=False)}")

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            settings.GROK_WS_URL,
            headers=headers,
            heartbeat=20,
            receive_timeout=45,
        ) as websocket:
            await websocket.send_json(payload)

            seen_types = set()
            start = time.time()

            while time.time() - start < 25:
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=6)
                except asyncio.TimeoutError:
                    print("receive_timeout=no new message")
                    break
                if message.type != aiohttp.WSMsgType.TEXT:
                    print(f"non-text={message.type}")
                    continue

                data = json.loads(message.data)
                message_type = data.get("type", "<none>")

                if message_type not in seen_types:
                    seen_types.add(message_type)
                    print(f"type={message_type}")

                if message_type == "error":
                    print(
                        f"error_code={data.get('err_code')} error_msg={data.get('err_msg')}"
                    )
                    break

                if message_type in ("image", "video") and data.get("url"):
                    url = data["url"]
                    print(f"url_sample={url[:140]}")

                if len(seen_types) >= 8:
                    break


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        default="all",
        choices=[
            "all",
            "image_default",
            "video_is_video",
            "video_generation_type",
            "video_output_type",
            "video_mode",
        ],
    )
    args = parser.parse_args()

    cases = [
        ("image_default", {}),
        ("video_is_video", {"is_video": True}),
        ("video_generation_type", {"generation_type": "video"}),
        ("video_output_type", {"output_type": "video"}),
        ("video_mode", {"mode": "video"}),
    ]

    if args.case != "all":
        cases = [case for case in cases if case[0] == args.case]

    for name, properties in cases:
        try:
            await run_case(name, properties)
        except Exception as error:
            print(f"[{name}] exception={type(error).__name__}: {error}")


if __name__ == "__main__":
    asyncio.run(main())
