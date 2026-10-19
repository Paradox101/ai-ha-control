import base64
import json
import io

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from app.config import Config
from app.llm.ollama import IMAGE_MAX_SIZE, IMAGE_RETRY_SIZE, OllamaClient, shrink_image
from app.tools.home import build_registry

OOM = '{"error":"an error was encountered while running the model: CUDA error\\nCUDA error: out of memory"}'


def jpeg(size):
    out = io.BytesIO()
    Image.new("RGB", size, (90, 120, 60)).save(out, "JPEG")
    return out.getvalue()


def image_size(data):
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_shrink_image():
    assert image_size(shrink_image(jpeg((3840, 2160)), IMAGE_MAX_SIZE)) == (1280, 720)
    small = jpeg((640, 360))
    assert shrink_image(small, IMAGE_MAX_SIZE) is small
    assert shrink_image(b"geen jpeg", IMAGE_MAX_SIZE) == b"geen jpeg"


async def test_out_of_memory_unloads_chat_model_and_retries_smaller(tmp_path):
    chats, unloaded = [], []

    async def chat(request):
        chats.append(await request.json())
        if len(chats) == 1:
            return web.Response(status=500, text=OOM)
        return web.json_response({"message": {"role": "assistant", "content": "Een kat op het gras."}})

    async def ps(_request):
        return web.json_response({"models": [{"name": "qwen3:8b"}, {"name": "qwen3-vl:4b"}]})

    async def generate(request):
        unloaded.append(await request.json())
        return web.json_response({"done": True})

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/api/ps", ps)
    app.router.add_post("/api/generate", generate)
    server = TestServer(app)
    await server.start_server()
    try:
        config = Config(ollama_url=str(server.make_url("")).rstrip("/"), data_dir=str(tmp_path))
        async with aiohttp.ClientSession() as session:
            llm = OllamaClient(config, session)
            assert await llm.describe_image(jpeg((3840, 2160)), "Wat zie je?") == "Een kat op het gras."
    finally:
        await server.close()
    assert unloaded == [{"model": "qwen3:8b", "keep_alive": 0}]
    sizes = [image_size(base64.b64decode(c["messages"][0]["images"][0])) for c in chats]
    assert sizes == [(IMAGE_MAX_SIZE, 720), (IMAGE_RETRY_SIZE, 432)]
    assert all(c["options"]["num_ctx"] == 4096 and c["model"] == "qwen3-vl:4b" for c in chats)


async def test_too_large_image_retries_smaller(tmp_path):
    chats = []

    async def chat(request):
        chats.append(await request.json())
        if len(chats) == 1:
            return web.Response(status=400, text='{"error":"request (11194 tokens) exceeds the available '
                                                 'context size (8192 tokens), try increasing it"}')
        return web.json_response({"message": {"role": "assistant", "content": "Niemand."}})

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    server = TestServer(app)
    await server.start_server()
    try:
        config = Config(ollama_url=str(server.make_url("")).rstrip("/"), data_dir=str(tmp_path))
        async with aiohttp.ClientSession() as session:
            assert await OllamaClient(config, session).describe_image(jpeg((3840, 2160)), "?") == "Niemand."
    finally:
        await server.close()
    assert len(chats) == 2


async def test_camera_shown_even_if_analysis_fails(ctx):
    async def broken(image, question, priority=1):
        raise RuntimeError(f"Ollama returned 500: {OOM}")
    ctx.llm.describe_image = broken
    result = json.loads(await build_registry().run(ctx, "analyze_camera", {"entity_id": "camera.voordeur_fluent"}))
    assert result["images"] == ["camera.voordeur_clear"]
    assert result["results"][0]["error"] == "beeldanalyse mislukt: te weinig GPU-geheugen voor het beeldmodel"
