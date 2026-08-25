#!/usr/bin/env python3
"""Sample: Wake word + Gemini Live + Agent brain — architecture skeleton.

This is a simplified reference showing the integration pattern.
Not a runnable production script — see README for full context.

Requirements:
    pip install websockets sounddevice numpy openwakeword
"""

import asyncio
import base64
import json
import os
import threading

import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Config — all secrets come from environment variables
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY", "")
AGENT_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8642")
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
MODEL = "gemini-3.1-flash-live-preview"

INPUT_RATE = 16000   # Gemini expects 16kHz input
OUTPUT_RATE = 24000  # Gemini sends 24kHz output
CHUNK_MS = 100
IDLE_TIMEOUT = 30    # seconds before auto-disconnect


# ---------------------------------------------------------------------------
# Audio Player — continuous output stream with ring buffer (no gaps)
# ---------------------------------------------------------------------------
class AudioPlayer:
    """Plays PCM16 audio through a continuous OutputStream.

    Using per-chunk sd.play() creates choppy audio. A continuous stream
    with a ring buffer callback eliminates gaps between chunks.
    """

    def __init__(self, sample_rate=OUTPUT_RATE):
        self._rate = sample_rate
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            callback=self._callback,
            blocksize=1024,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):
        needed = frames * 2  # int16 = 2 bytes per sample
        with self._lock:
            if len(self._buffer) >= needed:
                data = bytes(self._buffer[:needed])
                del self._buffer[:needed]
            else:
                data = bytes(self._buffer) + b"\x00" * (needed - len(self._buffer))
                self._buffer.clear()
        outdata[:] = np.frombuffer(data, dtype=np.int16).reshape(-1, 1)

    def write(self, pcm_bytes: bytes):
        with self._lock:
            self._buffer.extend(pcm_bytes)

    def clear(self):
        with self._lock:
            self._buffer.clear()


# ---------------------------------------------------------------------------
# Wake Word Detection
# ---------------------------------------------------------------------------
def wait_for_wake_word(model_name="hey_jarvis_v0.1", threshold=0.5):
    """Block until wake word is detected. Runs entirely locally (ONNX).

    Uses OpenWakeWord for zero-cost, always-on listening.
    Returns when wake word confidence exceeds threshold for 3+ consecutive frames.
    """
    from openwakeword.model import Model

    oww = Model(wakeword_models=[model_name])
    chunk_size = 1280  # 80ms at 16kHz

    consecutive_hits = 0
    required_hits = 3

    print("Listening for wake word...")

    with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                        blocksize=chunk_size) as stream:
        while True:
            audio, _ = stream.read(chunk_size)
            predictions = oww.predict(audio.flatten())

            if predictions.get(model_name, 0) >= threshold:
                consecutive_hits += 1
                if consecutive_hits >= required_hits:
                    print("Wake word detected!")
                    return
            else:
                consecutive_hits = 0


# ---------------------------------------------------------------------------
# Gemini Live Session
# ---------------------------------------------------------------------------
async def run_gemini_session():
    """Connect to Gemini Live, stream audio bidirectionally.

    Key patterns:
    1. Wait for setupComplete before streaming audio (prevents 1008 rejection)
    2. Tool calls and speech must be in the SAME turn
    3. Idle watchdog auto-disconnects to stop billing
    4. tool_in_progress flag prevents disconnect during agent calls
    """
    import websockets

    ws_url = (
        f"wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={API_KEY}"
    )

    player = AudioPlayer()
    setup_done = asyncio.Event()
    tool_in_progress = False
    last_activity = asyncio.get_event_loop().time()

    # --- Tool declaration for Gemini function calling ---
    tools = [
        {
            "functionDeclarations": [
                {
                    "name": "ask_agent",
                    "description": (
                        "Send a task to the AI agent. It has web search, "
                        "file ops, code execution, memory, and custom skills."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "task": {
                                "type": "STRING",
                                "description": "The task to perform.",
                            },
                            "fire_and_forget": {
                                "type": "BOOLEAN",
                                "description": (
                                    "If true, return immediately without "
                                    "waiting for the result."
                                ),
                            },
                        },
                        "required": ["task"],
                    },
                },
            ]
        }
    ]

    # --- System instruction ---
    system_instruction = (
        "You are a voice assistant. For tasks requiring tools, call ask_agent "
        "in the SAME turn as your speech acknowledgment. Never speak a full "
        "sentence before the tool call — say 2-3 words max then call the tool."
    )

    async with websockets.connect(ws_url) as ws:
        # Send setup message
        setup_msg = {
            "setup": {
                "model": f"models/{MODEL}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": system_instruction}]
                },
                "tools": tools,
            }
        }
        await ws.send(json.dumps(setup_msg))

        async def send_audio():
            """Stream mic audio to Gemini after setup completes."""
            await setup_done.wait()  # Gate on setupComplete
            chunk_samples = INPUT_RATE * CHUNK_MS // 1000

            with sd.InputStream(samplerate=INPUT_RATE, channels=1,
                                dtype="int16", blocksize=chunk_samples) as mic:
                while True:
                    audio, _ = mic.read(chunk_samples)
                    encoded = base64.b64encode(audio.tobytes()).decode()
                    msg = {"realtimeInput": {"mediaChunks": [
                        {"mimeType": "audio/pcm;rate=16000", "data": encoded}
                    ]}}
                    await ws.send(json.dumps(msg))
                    await asyncio.sleep(CHUNK_MS / 1000)

        async def receive_and_play():
            """Receive Gemini responses: audio, text, tool calls."""
            nonlocal tool_in_progress, last_activity

            async for raw in ws:
                msg = json.loads(raw)
                last_activity = asyncio.get_event_loop().time()

                # Setup acknowledgment
                if "setupComplete" in msg:
                    print("Gemini Live connected")
                    setup_done.set()
                    continue

                resp = msg.get("serverContent", {})

                # Audio output
                for part in resp.get("modelTurn", {}).get("parts", []):
                    if "inlineData" in part:
                        pcm = base64.b64decode(part["inlineData"]["data"])
                        player.write(pcm)

                # Barge-in: user interrupted
                if resp.get("interrupted"):
                    player.clear()

                # Tool calls
                tool_call = msg.get("toolCall", {})
                for fc in tool_call.get("functionCalls", []):
                    if fc["name"] == "ask_agent":
                        tool_in_progress = True
                        args = fc.get("args", {})
                        result = await call_agent(args.get("task", ""))
                        tool_in_progress = False

                        # Send tool response back to Gemini
                        tool_resp = {
                            "toolResponse": {
                                "functionResponses": [{
                                    "id": fc["id"],
                                    "name": fc["name"],
                                    "response": {"result": result},
                                }]
                            }
                        }
                        await ws.send(json.dumps(tool_resp))

        async def idle_watchdog():
            """Auto-disconnect after idle timeout (saves billing)."""
            nonlocal last_activity
            while True:
                await asyncio.sleep(5)
                if tool_in_progress:
                    continue
                elapsed = asyncio.get_event_loop().time() - last_activity
                if elapsed > IDLE_TIMEOUT:
                    print("Idle timeout — disconnecting")
                    return

        await asyncio.gather(
            send_audio(),
            receive_and_play(),
            idle_watchdog(),
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Agent Communication
# ---------------------------------------------------------------------------
async def call_agent(task: str) -> str:
    """Send a task to the agent's OpenAI-compatible API."""
    import aiohttp

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AGENT_API_KEY}",
    }
    payload = {
        "model": "default",
        "messages": [{"role": "user", "content": task}],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{AGENT_URL}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
def main():
    """Wake word → Gemini session → repeat."""
    print("Voice Agent Starting...")
    print(f"Model: {MODEL}")
    print(f"Agent URL: {AGENT_URL}")

    while True:
        wait_for_wake_word()
        try:
            asyncio.run(run_gemini_session())
        except Exception as e:
            print(f"Session error: {e}")
        print("Session ended. Returning to wake word listening.\n")


if __name__ == "__main__":
    main()
