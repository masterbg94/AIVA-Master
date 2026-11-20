#!/usr/bin/env python3
import asyncio
import math
import os
import shutil
import struct
import subprocess
import tempfile
from collections import deque
from datetime import timedelta
from pathlib import Path

import sim_zubar7 as agent

HOST, PORT = "127.0.0.1", 9092
FRAME = 320                       # 20 ms, mono, 8 kHz, 16 bit
RMS_THRESHOLD = int(os.getenv("AIVA_RMS_THRESHOLD", "250"))
END_SILENCE = int(os.getenv("AIVA_END_SILENCE_MS", "900")) // 20
MAX_TURNS = 12

# Za ovaj pilot transkripcija i LLM ostaju lokalni.
agent.WHISPER_URL = "http://127.0.0.1:8081/inference"
agent.LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"
agent.HTTP_TIMEOUT = 12


class TestCalendar:
    """Simulira raspoloživost; ne otvara SQLite i ne pristupa Google-u."""

    def __init__(self):
        self.booking_attempted = False

    def get_free_slots(self, day):
        if day.weekday() not in agent.WORK_DAYS:
            return []
        return [s for s in agent.DAILY_SLOTS if agent.is_slot_in_future(day, s)]

    def find_next_available_days(self, start_from, count=2, max_days=14):
        result = []
        for i in range(max_days):
            day = start_from + timedelta(days=i)
            slots = self.get_free_slots(day)
            if slots:
                result.append((day, slots))
                if len(result) >= count:
                    break
        return result

    def book_slot(self, *_):
        self.booking_attempted = True
        return False


def tts_pcm(text, folder):
    wav = folder / "reply.wav"
    wav.unlink(missing_ok=True)
    if not agent._generate_piper_wav(text, wav):
        raise RuntimeError("Piper nije napravio WAV")
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav),
         "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
        check=True, capture_output=True, timeout=10,
    ).stdout


def process_turn(pcm, folder, state, calendar):
    wav = folder / "input.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        input=pcm, check=True, capture_output=True, timeout=10,
    )
    text = agent.transcribe(wav)
    print(f"[ASR] {'Transkript primljen' if text else 'Prazan transkript'}", flush=True)
    if not text:
        return "Nisam vas najbolje čuo. Možete li da ponovite?", False

    info = agent.extract(text, state["stage"])
    print("[LLM] Namera obrađena", flush=True)
    reply, done, transfer = agent.next_reply(info, state, text, calendar)
    if calendar.booking_attempted:
        return "Ovo je test poziv. Termin nije rezervisan. Hvala i doviđenja.", True
    if transfer:
        return "Preusmeravanje nije dostupno u ovom testu. Hvala i doviđenja.", True
    return reply, done


async def read_packet(reader):
    header = await reader.readexactly(3)
    size = int.from_bytes(header[1:], "big")
    return header[0], await reader.readexactly(size)


async def send_audio(writer, pcm):
    for offset in range(0, len(pcm), FRAME):
        frame = pcm[offset:offset + FRAME].ljust(FRAME, b"\x00")
        writer.write(b"\x10" + len(frame).to_bytes(2, "big") + frame)
        await asyncio.wait_for(writer.drain(), 2)
        await asyncio.sleep(0.020)


def rms(frame):
    samples = struct.unpack("<160h", frame)
    return math.sqrt(sum(s * s for s in samples) / 160)


async def listen(queue):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 8
    pre_roll = deque(maxlen=15)
    speech = []
    pending = bytearray()
    voiced = silence = 0

    while (remaining := deadline - loop.time()) > 0:
        try:
            pending.extend(await asyncio.wait_for(queue.get(), remaining))
        except TimeoutError:
            break

        while len(pending) >= FRAME:
            frame = bytes(pending[:FRAME])
            del pending[:FRAME]
            loud = rms(frame) >= RMS_THRESHOLD

            if not speech:
                pre_roll.append(frame)
                voiced = voiced + 1 if loud else 0
                if voiced >= 3:
                    speech = list(pre_roll)
                    deadline = loop.time() + 12
            else:
                speech.append(frame)
                silence = 0 if loud else silence + 1
                if silence >= END_SILENCE:
                    return b"".join(speech)

    # Ako govor dođe do limita bez kraja, ne tumači odsečenu repliku.
    return b""


async def main():
    if not shutil.which("ffmpeg") or not shutil.which("piper"):
        raise SystemExit("Potrebni su ffmpeg i piper u PATH-u.")
    if not agent.PIPER_MODEL.is_file():
        raise SystemExit(f"Nedostaje Piper model: {agent.PIPER_MODEL}")
    if RMS_THRESHOLD <= 0 or END_SILENCE < 1:
        raise SystemExit("Proveri podešavanja za detekciju govora.")

    with tempfile.TemporaryDirectory(prefix="aiva-start-") as tmp:
        folder = Path(tmp)
        greeting = await asyncio.to_thread(
            tts_pcm,
            "Dobar dan. Ovo je test glasovnog asistenta ordinacije Osmeh. "
            "Koristite samo izmišljene podatke. Kako mogu da vam pomognem?",
            folder,
        )
        error_audio = await asyncio.to_thread(
            tts_pcm, "Trenutno ne mogu da obradim poziv. Doviđenja.", folder
        )

    busy = False

    async def handle_call(reader, writer):
        nonlocal busy
        if busy:
            writer.close()
            return
        busy = True
        queue = asyncio.Queue(maxsize=100)
        listening = asyncio.Event()
        disconnected = asyncio.Event()
        receiver = None

        async def receive():
            try:
                while True:
                    kind, payload = await asyncio.wait_for(read_packet(reader), 30)
                    if kind in (0x00, 0xFF):
                        return
                    if kind == 0x10 and listening.is_set() and payload:
                        queue.put_nowait(payload)
            except (asyncio.IncompleteReadError, TimeoutError, OSError, asyncio.QueueFull):
                pass
            finally:
                disconnected.set()

        async def next_utterance():
            listening.set()
            speech_task = asyncio.create_task(listen(queue))
            hangup_task = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait(
                    {speech_task, hangup_task}, return_when=asyncio.FIRST_COMPLETED
                )
                return b"" if disconnected.is_set() else speech_task.result()
            finally:
                listening.clear()
                for task in (speech_task, hangup_task):
                    task.cancel()
                await asyncio.gather(speech_task, hangup_task, return_exceptions=True)
                while not queue.empty():
                    queue.get_nowait()

        try:
            kind, payload = await asyncio.wait_for(read_packet(reader), 5)
            if kind != 0x01 or len(payload) != 16:
                return
            print("[SIP] Novi poziv", flush=True)
            receiver = asyncio.create_task(receive())
            state = {
                "stage": "slot", "date": None, "time": None,
                "name": None, "phone": None, "offered_dates": [], "fails": 0,
            }
            calendar = TestCalendar()
            misses = 0

            with tempfile.TemporaryDirectory(prefix="aiva-call-") as tmp:
                folder = Path(tmp)
                await send_audio(writer, greeting)

                for _ in range(MAX_TURNS):
                    if disconnected.is_set():
                        break
                    pcm = await next_utterance()
                    if disconnected.is_set():
                        break

                    if pcm:
                        misses = 0
                        print(f"[VAD] Replika: {len(pcm) / 16000:.1f} s", flush=True)
                        reply, done = await asyncio.to_thread(
                            process_turn, pcm, folder, state, calendar
                        )
                    else:
                        misses += 1
                        done = misses >= 2
                        reply = (
                            "Ne čujem vas dovoljno jasno. Doviđenja."
                            if done else "Nisam vas čuo. Možete li da ponovite?"
                        )

                    if disconnected.is_set():
                        break
                    audio = await asyncio.to_thread(tts_pcm, reply, folder)
                    if disconnected.is_set():
                        break
                    await send_audio(writer, audio)
                    if done:
                        break

        except (asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        except Exception as exc:
            print(f"[SIP] Obrada nije uspela: {type(exc).__name__}", flush=True)
            if not disconnected.is_set():
                try:
                    await send_audio(writer, error_audio)
                except (OSError, TimeoutError):
                    pass
        finally:
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass
            busy = False
            print("[SIP] Poziv završen", flush=True)

    server = await asyncio.start_server(handle_call, HOST, PORT)
    print(f"[SIP] Slušam na {HOST}:{PORT}; test režim, bez rezervacija", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
