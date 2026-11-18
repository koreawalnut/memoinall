"""One authenticated Gemini connection per local microphone session."""
import asyncio
import base64
import json
from contextlib import asynccontextmanager

from fastapi import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import settings, stt

URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


@asynccontextmanager
async def connection(key=""):
    key = key or settings.provider_config("gemini").get("api_key")
    if not key:
        raise stt.SttError("Gemini API 키를 입력하세요.")
    try:
        async with connect(URL, additional_headers={"x-goog-api-key": key}, open_timeout=10, close_timeout=2) as cloud:
            await cloud.send(json.dumps({"setup": {
                "model": "models/" + stt.LIVE_MODEL,
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": {"languageCodes": ["ko-KR"]},
                "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
            }}))
            reply = json.loads(await asyncio.wait_for(cloud.recv(), 10))
            if "setupComplete" not in reply:
                raise stt.SttError("Gemini Live가 연결을 승인하지 않았습니다. API 키와 모델 사용 권한을 확인하세요.")
            yield cloud
    except (ConnectionClosed, InvalidStatus, OSError, asyncio.TimeoutError) as exc:
        raise stt.SttError("Gemini Live 연결이 끊겼거나 응답이 지연됐습니다. 네트워크, API 키와 Live 모델 사용 권한을 확인하세요.") from exc


async def relay(client, cloud):
    pending = 0
    finalized = asyncio.Event()
    finalized.set()

    async def microphone():
        nonlocal pending
        active = False
        size = 0
        while True:
            frame = await client.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            audio = frame.get("bytes")
            command = frame.get("text")
            if audio is not None and active and 0 < len(audio) <= 16384 and len(audio) % 2 == 0:
                size += len(audio)
                if size > stt.MAX_BYTES:
                    raise stt.SttError("녹음 가능 시간을 넘겼습니다.")
                message = {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(audio).decode("ascii")}}
            elif command == "start" and not active:
                active = True
                pending += 1
                finalized.clear()
                message = {"activityStart": {}}
            elif command == "end" and active:
                active = False
                message = {"activityEnd": {}}
            elif command == "finish" and not active:
                try:
                    await asyncio.wait_for(finalized.wait(), 12)
                except asyncio.TimeoutError as exc:
                    raise stt.SttError("마지막 구간의 확정 결과를 받지 못했습니다.") from exc
                await client.send_json({"done": True})
                return
            else:
                raise stt.SttError("음성 입력 세션의 오디오 형식을 확인하세요.")
            await cloud.send(json.dumps({"realtimeInput": message}))

    async def transcripts():
        nonlocal pending
        while True:
            msg = json.loads(await cloud.recv())
            if "error" in msg:
                raise stt.SttError("Gemini Live에서 음성을 처리하지 못했습니다.")
            content = msg.get("serverContent", {})
            if "inputTranscription" in content:
                await client.send_json({"final": content["inputTranscription"].get("text", "")})
                pending = max(0, pending - 1)
                if not pending:
                    finalized.set()
            elif "interimInputTranscription" in content:
                await client.send_json({"interim": content["interimInputTranscription"].get("text", "")})

    await client.send_json({"ready": True})
    tasks = [asyncio.create_task(microphone()), asyncio.create_task(transcripts())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=stt.MAX_SECONDS + 30, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise stt.SttError("음성 입력 세션 시간이 끝났습니다.")
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
