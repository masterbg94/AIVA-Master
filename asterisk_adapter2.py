#!/usr/bin/env python3
"""
asterisk_adapter.py  (v2)  -  Asterisk AudioSocket <-> lokalni AI agent (sim_zubar7)

Fokus ove verzije: najbolji moguci IZLAZNI ZVUK na 8 kHz telefonskom kanalu.

  * jedan neprekidan 20 ms "sat" (Player) koji stalno salje okvire (tisinu kad nema govora)
  * TCP_NODELAY, apsolutno merenje vremena (nema driftovanja)
  * TTS u izvornoj frekvenciji -> filteri -> soxr resampling na 8 kHz -> normalizacija -> fade
  * recenicu po recenicu: dok svira prva, sintetise se druga
  * kes gotovih replika, zagrevanje pri startu
  * ispravke dijaloga: latinica, potvrda "da/ne", telefon izgovoren recima, bez rusenja poziva

Pokretanje (test, bez rezervacija):      python3 asterisk_adapter.py
Pokretanje sa pravim zakazivanjem:       AIVA_LIVE_BOOKING=1 python3 asterisk_adapter.py
"""
import asyncio
import inspect
import math
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from array import array
from collections import OrderedDict, deque
from datetime import timedelta
from pathlib import Path

import sim_zubar7 as agent

# ----------------------------------------------------------------------------
# Podesavanja
# ----------------------------------------------------------------------------
HOST, PORT = "127.0.0.1", 9092
FRAME = 320                         # 20 ms, mono, 8 kHz, 16 bit
FRAME_S = 0.020
SILENCE = bytes(FRAME)
AUDIO_HDR = b"\x10" + FRAME.to_bytes(2, "big")

RMS_THRESHOLD = int(os.getenv("AIVA_RMS_THRESHOLD", "250"))
END_SILENCE = int(os.getenv("AIVA_END_SILENCE_MS", "900")) // 20
MAX_TURNS = int(os.getenv("AIVA_MAX_TURNS", "20"))

TTS_MODE = os.getenv("AIVA_TTS", "edge")            # "edge" (Piper rezerva) ili "piper"
PIPER_CYR = os.getenv("AIVA_PIPER_CYR", "0") == "1"  # A/B test: cirilica u Piper
TARGET_RMS = int(os.getenv("AIVA_TARGET_RMS", "2600"))   # oko -22 dBFS
PEAK_LIMIT = int(os.getenv("AIVA_PEAK", "26000"))        # oko -2 dBFS
LIVE_BOOKING = os.getenv("AIVA_LIVE_BOOKING", "0") == "1"
DUMP_DIR = os.getenv("AIVA_DUMP_DIR", "")            # npr. /tmp/aiva  -> snima poslati zvuk

# agent.WHISPER_URL = os.getenv("WHISPER_URL", "http://127.0.0.1:8081/inference")
# agent.LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:8082/v1/chat/completions")
agent.HTTP_TIMEOUT = 12
if os.getenv("AIVA_PIPER_EXTRA"):                   # npr. "--length_scale 1.05" (proveri: piper --help)
    agent.PIPER_EXTRA = shlex.split(os.environ["AIVA_PIPER_EXTRA"])

# Filteri pre spustanja na 8 kHz (radi se na 22/24 kHz materijalu):
#  - highpass: uklanja bubnjanje; lowpass: telefonski opseg i anti-aliasing
#  - blagi "presence" +2 dB za razumljivost; blaga kompresija za ujednacenu glasnocu
AF_BASE = (
    "highpass=f=90,lowpass=f=3500,"
    "equalizer=f=2600:t=q:w=1:g=2,"
    "acompressor=threshold=0.125:ratio=3:attack=5:release=80"
)
_soxr_ok = True


# ----------------------------------------------------------------------------
# Kalendar za test
# ----------------------------------------------------------------------------
class TestCalendar:
    """Simulira raspolozivost; ne otvara SQLite i ne pristupa Google-u."""

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


# ----------------------------------------------------------------------------
# Tekst: latinica, "da/ne", telefon recima, ime
# ----------------------------------------------------------------------------
_LOW = "абвгдђежзијклмнопрстћуфхцчш"
_LAT = "abvgdđežzijklmnoprstćufhcčš"
_CYR2LAT = str.maketrans(_LOW + _LOW.upper(), _LAT + _LAT.upper())
_DIGITS = {
    "nula": "0", "jedan": "1", "jedna": "1", "dva": "2", "dve": "2", "tri": "3",
    "četiri": "4", "cetiri": "4", "pet": "5", "šest": "6", "sest": "6",
    "sedam": "7", "osam": "8", "devet": "9",
}
YES_RE = re.compile(r"\b(da|može|moze|naravno|u redu|tačno|tacno|važi|vazi|zakaži|zakazi)\b", re.I)
NO_RE = re.compile(r"\b(ne|nemoj|promeni|drugi|drugo)\b", re.I)


def cyr_to_lat(text: str) -> str:
    for c, l in {"љ": "lj", "њ": "nj", "џ": "dž", "Љ": "Lj", "Њ": "Nj", "Џ": "Dž"}.items():
        text = text.replace(c, l)
    return text.translate(_CYR2LAT)


def spoken_phone(text: str):
    out = []
    for tok in re.findall(r"[a-zčćžšđ]+|\d+", text.lower()):
        out.append(tok if tok.isdigit() else _DIGITS.get(tok, ""))
    digits = "".join(out)
    return agent.normalize_phone(digits) if len(digits) >= 8 else None


def fallback_name(text: str):
    if re.search(r"\d", text):
        return None
    t = re.sub(r"^(zovem se|ja sam|moje ime je|ime mi je)\s+", "", text.strip(), flags=re.I)
    t = re.sub(r"[^A-Za-zčćžšđČĆŽŠĐ\s-]", "", t).strip()
    words = t.split()
    if 2 <= len(words) <= 3:
        return " ".join(w.capitalize() for w in words)
    return None


# ----------------------------------------------------------------------------
# TTS -> 8 kHz PCM (ovde se resava kvalitet izlaznog zvuka)
# ----------------------------------------------------------------------------
def synth_wav(text: str, wav: Path) -> bool:
    clean = re.sub(r"[^\w\s.,!?čćžšđČĆŽŠĐ-]", "", text).strip()
    if not clean:
        return False
    wav.unlink(missing_ok=True)
    if TTS_MODE == "edge" and agent._generate_edge_tts_wav(clean, wav):
        return True
    piper_text = agent.latin_to_cyrillic(clean) if PIPER_CYR else clean
    return agent._generate_piper_wav(piper_text, wav)


def wav_to_pcm8k(wav: Path) -> bytes:
    global _soxr_ok
    chains = []
    if _soxr_ok:
        chains.append(AF_BASE + ",aresample=osr=8000:resampler=soxr")
    chains.append(AF_BASE + ",aresample=osr=8000")
    last_exc = None
    for chain in chains:
        try:
            return subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav),
                 "-af", chain, "-ac", "1", "-ar", "8000",
                 "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                check=True, capture_output=True, timeout=15,
            ).stdout
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if "soxr" in chain:
                _soxr_ok = False
    raise last_exc


def finalize(pcm: bytes, lead_ms: int, tail_ms: int) -> bytes:
    """Normalizacija glasnoce bez clippinga, fade in/out (nema klikova), tisina na pocetku/kraju."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    n = len(a)
    if n == 0:
        return b""
    peak = max(max(a), -min(a)) or 1
    rms = math.sqrt(sum(x * x for x in a) / n) or 1.0
    gain = min(TARGET_RMS / rms, PEAK_LIMIT / peak, 8.0)
    out = array("h", (max(-32768, min(32767, int(x * gain))) for x in a))
    fade = min(80, n // 2)                               # 10 ms
    for i in range(fade):
        g = i / fade
        out[i] = int(out[i] * g)
        out[n - 1 - i] = int(out[n - 1 - i] * g)
    if sys.byteorder == "big":
        out.byteswap()
    return bytes(8 * lead_ms * 2) + out.tobytes() + bytes(8 * tail_ms * 2)


_cache: "OrderedDict[tuple, bytes]" = OrderedDict()


def tts_pcm(text: str, folder: Path, lead_ms: int = 60, tail_ms: int = 90) -> bytes:
    key = (TTS_MODE, PIPER_CYR, text, lead_ms, tail_ms)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    wav = folder / "reply.wav"
    if not synth_wav(text, wav):
        raise RuntimeError("TTS nije napravio WAV")
    pcm = finalize(wav_to_pcm8k(wav), lead_ms, tail_ms)
    _cache[key] = pcm
    if len(_cache) > 300:
        _cache.popitem(last=False)
    if DUMP_DIR:
        Path(DUMP_DIR).mkdir(parents=True, exist_ok=True)
        (Path(DUMP_DIR) / f"{abs(hash(key)) % 10**8}.raw").write_bytes(pcm)
    return pcm


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) < 14:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged or [text]


# ----------------------------------------------------------------------------
# Player: jedan stabilan 20 ms sat za slanje ka Asterisku
# ----------------------------------------------------------------------------
class Player:
    def __init__(self, writer, disconnected: asyncio.Event):
        self.writer = writer
        self.disconnected = disconnected
        self.buf: deque[bytes] = deque()
        self.idle = asyncio.Event()
        self.idle.set()
        self.task: asyncio.Task | None = None

    def start(self):
        self.task = asyncio.create_task(self._run())

    def enqueue(self, pcm: bytes):
        for off in range(0, len(pcm), FRAME):
            self.buf.append(pcm[off:off + FRAME].ljust(FRAME, b"\x00"))
        if self.buf:
            self.idle.clear()

    def clear(self):
        self.buf.clear()
        self.idle.set()

    async def wait_idle(self):
        waiter = asyncio.create_task(self.idle.wait())
        gone = asyncio.create_task(self.disconnected.wait())
        try:
            await asyncio.wait({waiter, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (waiter, gone):
                t.cancel()
            await asyncio.gather(waiter, gone, return_exceptions=True)

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        try:
            while True:
                frame = self.buf.popleft() if self.buf else SILENCE
                self.writer.write(AUDIO_HDR + frame)
                await asyncio.wait_for(self.writer.drain(), 2)
                if not self.buf:
                    self.idle.set()
                next_t += FRAME_S
                delay = next_t - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.2:                      # zaostali smo (CPU spike) - resetuj sat
                    next_t = loop.time()
        except (OSError, TimeoutError, ConnectionError):
            self.disconnected.set()
        except asyncio.CancelledError:
            pass
        finally:
            self.idle.set()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def speak(player: Player, text: str, folder: Path, disconnected: asyncio.Event):
    """Sintetise recenicu po recenicu; prva pocinje da svira dok se druga jos pravi."""
    for i, part in enumerate(split_sentences(text)):
        if disconnected.is_set():
            return
        pcm = await asyncio.to_thread(tts_pcm, part, folder, 60 if i == 0 else 0, 90)
        player.enqueue(pcm)
    await player.wait_idle()
    await asyncio.sleep(0.15)                            # ne slusaj sopstveni "rep"


# ----------------------------------------------------------------------------
# Obrada jedne replike pacijenta
# ----------------------------------------------------------------------------
_EXTRACT_TAKES_STATE = len(inspect.signature(agent.extract).parameters) >= 3


def process_turn(pcm, folder, state, calendar):
    wav = folder / "input.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
         "-af", "highpass=f=100,lowpass=f=3600,dynaudnorm=f=150:g=7",
         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        input=pcm, check=True, capture_output=True, timeout=10,
    )
    text = cyr_to_lat(agent.transcribe(wav))
    print(f"[ASR] {text!r}", flush=True)                 # u produkciji ukloni (lični podaci)
    if not text:
        return "Nisam vas najbolje čuo. Možete li da ponovite?", False

    try:
        if _EXTRACT_TAKES_STATE:
            info = agent.extract(text, state["stage"], state)
        else:
            info = agent.extract(text, state["stage"])
    except Exception as exc:
        print(f"[LLM] greška: {type(exc).__name__}", flush=True)
        info = {"intent": "other"}

    if state["stage"] == "confirm":
        if NO_RE.search(text):
            info.update(intent="book", confirm="no")
        elif YES_RE.search(text):
            info.update(intent="book", confirm="yes")

    if state["date"] and state["time"] and not state["name"] and not agent.validate_name(info.get("name")):
        guess = fallback_name(text)
        if guess:
            info.update(name=guess, intent="book")
    if state["name"] and not state["phone"] and not agent.normalize_phone(info.get("phone")):
        guess = spoken_phone(text)
        if guess:
            info.update(phone=guess, intent="book")

    print(f"[LLM] {info}", flush=True)
    reply, done, transfer = agent.next_reply(info, state, text, calendar)
    print(f"[STATE] {state}", flush=True)
    if getattr(calendar, "booking_attempted", False):
        return "Ovo je test poziv. Termin nije rezervisan. Hvala i doviđenja.", True
    if transfer:
        return "Preusmeravanje nije dostupno u ovom testu. Hvala i doviđenja.", True
    return reply, done


# ----------------------------------------------------------------------------
# AudioSocket + VAD
# ----------------------------------------------------------------------------
async def read_packet(reader):
    header = await reader.readexactly(3)
    size = int.from_bytes(header[1:], "big")
    return header[0], await reader.readexactly(size)


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

    return b""


async def warm_cache(folder: Path, texts: list[str]):
    for text in texts:
        for i, part in enumerate(split_sentences(text)):
            await asyncio.to_thread(tts_pcm, part, folder, 60 if i == 0 else 0, 90)


GREETING = ("Dobar dan. Ovo je test glasovnog asistenta ordinacije Osmeh. "
            "Koristite samo izmišljene podatke. Kako mogu da vam pomognem?")
ERROR_TEXT = "Trenutno ne mogu da obradim poziv. Doviđenja."
MISS_1 = "Nisam vas čuo. Možete li da ponovite?"
MISS_2 = "Ne čujem vas dovoljno jasno. Doviđenja."


async def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("Potreban je ffmpeg u PATH-u.")
    if TTS_MODE != "edge" and not shutil.which("piper"):
        raise SystemExit("Potreban je piper u PATH-u.")
    if TTS_MODE == "piper" and not agent.PIPER_MODEL.is_file():
        raise SystemExit(f"Nedostaje Piper model: {agent.PIPER_MODEL}")
    if RMS_THRESHOLD <= 0 or END_SILENCE < 1:
        raise SystemExit("Proveri podešavanja za detekciju govora.")

    shared_calendar = agent.CalendarManager() if LIVE_BOOKING else None

    with tempfile.TemporaryDirectory(prefix="aiva-start-") as tmp:
        await warm_cache(Path(tmp), [
            GREETING, ERROR_TEXT, MISS_1, MISS_2,
            "Nisam vas najbolje čuo. Možete li da ponovite?",
            "Hvala na pozivu, doviđenja.",
        ])

    busy = False

    async def handle_call(reader, writer):
        nonlocal busy
        if busy:
            writer.close()
            return
        busy = True
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        queue = asyncio.Queue(maxsize=100)
        listening = asyncio.Event()
        disconnected = asyncio.Event()
        receiver = None
        player = Player(writer, disconnected)

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
            while not queue.empty():
                queue.get_nowait()
            listening.set()
            speech_task = asyncio.create_task(listen(queue))
            hangup_task = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait({speech_task, hangup_task}, return_when=asyncio.FIRST_COMPLETED)
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
            player.start()
            state = {
                "stage": "slot", "date": None, "time": None,
                "name": None, "phone": None, "offered_dates": [], "fails": 0,
            }
            calendar = shared_calendar or TestCalendar()
            misses = 0

            with tempfile.TemporaryDirectory(prefix="aiva-call-") as tmp:
                folder = Path(tmp)
                await speak(player, GREETING, folder, disconnected)

                for _ in range(MAX_TURNS):
                    if disconnected.is_set():
                        break
                    pcm = await next_utterance()
                    if disconnected.is_set():
                        break

                    if pcm:
                        misses = 0
                        print(f"[VAD] Replika: {len(pcm) / 16000:.1f} s", flush=True)
                        try:
                            reply, done = await asyncio.to_thread(
                                process_turn, pcm, folder, state, calendar
                            )
                        except Exception as exc:
                            print(f"[TURN] greška: {type(exc).__name__}: {exc}", flush=True)
                            reply, done = "Izvinite, možete li da ponovite?", False
                    else:
                        misses += 1
                        done = misses >= 2
                        reply = MISS_2 if done else MISS_1

                    if disconnected.is_set():
                        break
                    await speak(player, reply, folder, disconnected)
                    if done:
                        break

        except (asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        except Exception as exc:
            print(f"[SIP] Obrada nije uspela: {type(exc).__name__}: {exc}", flush=True)
            if not disconnected.is_set():
                try:
                    with tempfile.TemporaryDirectory(prefix="aiva-err-") as tmp:
                        await speak(player, ERROR_TEXT, Path(tmp), disconnected)
                except Exception:
                    pass
        finally:
            await player.stop()
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
    mode = "PRAVO zakazivanje" if LIVE_BOOKING else "test režim, bez rezervacija"
    print(f"[SIP] Slušam na {HOST}:{PORT}; {mode}; TTS={TTS_MODE}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass