#!/usr/bin/env python3
"""
asterisk_adapter3.py - Asterisk AudioSocket <-> lokalni AI agent (referenca: sim_zubar7 + asterisk_adapter2)

  * RAZGOVOR NA SRPSKOM: Whisper se zove sa language=sr + prompt; odbacuju se transkripti na pogresnom
    jeziku/pismu i poznate Whisper halucinacije.
  * KONZOLA: svaka replika - sta agent CUJE (PACIJENT) i sta agent KAZE (AGENT) + fajl call_logs/call_*.txt
  * REALAN POZIV: simulirano zakazivanje u memoriji (nista se ne upisuje u Google/SQLite)
  * ZVUK: Player sa 20 ms satom, filteri + blagi denoise + limiter, soxr 8 kHz, ujednacena glasnoca
    (numpy ako postoji), Piper poziv sa -f / --output_file rezervom.

Pokretanje:  AIVA_TTS=piper python3 -u asterisk_adapter3.py
Pravi upis:  AIVA_TTS=piper AIVA_LIVE_BOOKING=1 python3 -u asterisk_adapter3.py
Agent modul: AIVA_AGENT=sim_zubar7 (podrazumevano)
"""
import asyncio, importlib, inspect, json, math, os, re, shlex, shutil, socket, struct
import subprocess, sys, tempfile, threading, time as time_mod, urllib.request, uuid
from array import array
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

agent = importlib.import_module(os.getenv("AIVA_AGENT", "sim_zubar7"))

# ---------------------------------------------------------------- podesavanja
HOST, PORT = "127.0.0.1", 9092
FRAME, FRAME_S = 320, 0.020                       # 20 ms, 8 kHz, 16 bit mono
SILENCE = bytes(FRAME)
AUDIO_HDR = b"\x10" + FRAME.to_bytes(2, "big")

def env_int(n, d): return int(os.getenv(n, str(d)))

RMS_THRESHOLD = env_int("AIVA_RMS_THRESHOLD", 250)
END_SILENCE = env_int("AIVA_END_SILENCE_MS", 900) // 20
MAX_TURNS = env_int("AIVA_MAX_TURNS", 20)
TTS_MODE = os.getenv("AIVA_TTS", "piper")                  # piper (lokalno) | edge (internet)
PIPER_CYR = os.getenv("AIVA_PIPER_CYR", "0") == "1"
TARGET_RMS = env_int("AIVA_TARGET_RMS", 2600)
PEAK_LIMIT = env_int("AIVA_PEAK", 26000)
LIVE_BOOKING = os.getenv("AIVA_LIVE_BOOKING", "0") == "1"
DUMP_DIR = os.getenv("AIVA_DUMP_DIR", "")
LOG_DIR = Path(os.getenv("AIVA_LOG_DIR", "call_logs"))
LOG_TEXT = os.getenv("AIVA_LOG_TEXT", "1") == "1"
DEBUG = os.getenv("AIVA_DEBUG", "0") == "1"
SAVE_AUDIO = os.getenv("AIVA_SAVE_AUDIO", "0") == "1"
COLOR = os.getenv("AIVA_COLOR", "1") == "1" and sys.stdout.isatty()
DENOISE = os.getenv("AIVA_DENOISE", "1") == "1"             # blagi afftdn; =0 ako zvuci "pod vodom"
ASR_LANG = os.getenv("AIVA_ASR_LANG", "sr")
NO_SPEECH_THOLD = os.getenv("AIVA_NO_SPEECH_THOLD", "0.6")

agent.HTTP_TIMEOUT = 12
if os.getenv("AIVA_PIPER_EXTRA"):
    agent.PIPER_EXTRA = shlex.split(os.environ["AIVA_PIPER_EXTRA"])

AF_BASE = ("highpass=f=100,lowpass=f=3400,"
           + ("afftdn=nr=6:nf=-50,highpass=f=100," if DENOISE else "")
           + "equalizer=f=2600:t=q:w=1:g=2,"
           "acompressor=threshold=0.125:ratio=3:attack=5:release=80,"
           "alimiter=limit=0.85:attack=2:release=30:level=false")
_soxr_ok = True


# ---------------------------------------------------------------- konzola + transkript
def _c(code, s): return f"\033[{code}m{s}\033[0m" if COLOR else s


class CallLog:
    def __init__(self):
        self.t0 = time_mod.monotonic()
        self.lock = threading.Lock()
        self.file = None
        if LOG_TEXT:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.file = open(LOG_DIR / datetime.now().strftime("call_%Y%m%d_%H%M%S.txt"),
                             "a", encoding="utf-8", buffering=1)

    def _w(self, label, icon, color, text):
        if not LOG_TEXT:
            return
        t = time_mod.monotonic() - self.t0
        with self.lock:
            print(f"{_c('90', f'[{t:6.1f}s]')} {_c(color, f'{icon} {label}:')} {text}", flush=True)
            if self.file:
                self.file.write(f"[{t:6.1f}s] {label}: {text}\n")

    def heard(self, text): self._w("PACIJENT (agent cuje)", "👂", "96", text)
    def said(self, text): self._w("AGENT (agent kaze)", "🤖", "92", text)
    def info(self, text): self._w("INFO", "ℹ️ ", "93", text)

    def close(self):
        if self.file:
            self.file.close()


# ---------------------------------------------------------------- simulirani kalendar
class SimCalendar:
    _booked: set = set()
    _lock = threading.Lock()

    def get_free_slots(self, day):
        if day.weekday() not in agent.WORK_DAYS:
            return []
        with self._lock:
            taken = {s for (d, s) in self._booked if d == day}
        return [s for s in agent.DAILY_SLOTS if s not in taken and agent.is_slot_in_future(day, s)]

    def find_next_available_days(self, start_from, count=2, max_days=14):
        out = []
        for i in range(max_days):
            day = start_from + timedelta(days=i)
            slots = self.get_free_slots(day)
            if slots:
                out.append((day, slots))
                if len(out) >= count:
                    break
        return out

    def book_slot(self, target_date, slot_time, name, phone):
        if not agent.is_slot_in_future(target_date, slot_time):
            return False
        with self._lock:
            if (target_date, slot_time) in self._booked:
                return False
            self._booked.add((target_date, slot_time))
        print(f"[SIM-BOOKING] {target_date} {slot_time} (simulirano, nista nije upisano)", flush=True)
        return True


# ---------------------------------------------------------------- tekst
_LOW = "абвгдђежзијклмнопрстћуфхцчш"
_LAT = "abvgdđežzijklmnoprstćufhcčš"
_CYR2LAT = str.maketrans(_LOW + _LOW.upper(), _LAT + _LAT.upper())
_DIGITS = {"nula": "0", "jedan": "1", "jedna": "1", "dva": "2", "dve": "2", "tri": "3",
           "četiri": "4", "cetiri": "4", "pet": "5", "šest": "6", "sest": "6",
           "sedam": "7", "osam": "8", "devet": "9"}
YES_RE = re.compile(r"\b(da|može|moze|naravno|u redu|tačno|tacno|važi|vazi|zakaži|zakazi)\b", re.I)
NO_RE = re.compile(r"\b(ne|nemoj|promeni|drugi|drugo)\b", re.I)
HALLUCINATIONS = ("hvala što ste gledali", "hvala sto ste gledali", "thanks for watching",
                  "thank you for watching", "amara.org", "titlovi", "prevod:", "subscribe")
EN_WORDS = {"the", "you", "and", "this", "that", "is", "are", "thank", "thanks", "please", "with"}


def cyr_to_lat(text):
    for c, l in {"љ": "lj", "њ": "nj", "џ": "dž", "Љ": "Lj", "Њ": "Nj", "Џ": "Dž"}.items():
        text = text.replace(c, l)
    return text.translate(_CYR2LAT)


def looks_serbian(text):
    """True ako je transkript upotrebljiv srpski (latinica/cirilica), bez stranih pisama i halucinacija."""
    low = text.lower()
    if any(h in low for h in HALLUCINATIONS):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    ok = sum(1 for ch in letters if ch.isascii() or ch in "čćžšđČĆŽŠĐ" or "а" <= ch.lower() <= "ш" or ch.lower() in "ђјљњћџ")
    if ok / len(letters) < 0.9:
        return False
    words = re.findall(r"[a-z]+", low)
    return not (len(words) >= 3 and sum(w in EN_WORDS for w in words) >= 2)


def spoken_phone(text):
    out = []
    for tok in re.findall(r"[a-zčćžšđ]+|\d+", text.lower()):
        out.append(tok if tok.isdigit() else _DIGITS.get(tok, ""))
    d = "".join(out)
    return agent.normalize_phone(d) if len(d) >= 8 else None


def fallback_name(text):
    if re.search(r"\d", text):
        return None
    t = re.sub(r"^(zovem se|ja sam|moje ime je|ime mi je)\s+", "", text.strip(), flags=re.I)
    t = re.sub(r"[^A-Za-zčćžšđČĆŽŠĐ\s-]", "", t).strip()
    w = t.split()
    return " ".join(x.capitalize() for x in w) if 2 <= len(w) <= 3 else None


# ---------------------------------------------------------------- ASR (uvek srpski)
def transcribe_sr(wav: Path) -> str:
    b = "aiva-" + uuid.uuid4().hex
    fields = [("response_format", "json"), ("language", ASR_LANG), ("temperature", "0"),
              ("no_speech_thold", NO_SPEECH_THOLD), ("suppress_nst", "true"),
              ("prompt", getattr(agent, "WHISPER_PROMPT", ""))]
    body = bytearray()
    for k, v in fields:
        body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="in.wav"\r\n'
             'Content-Type: audio/wav\r\n\r\n').encode() + wav.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(agent.WHISPER_URL, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=agent.HTTP_TIMEOUT) as r:
        return json.load(r).get("text", "").strip()


# ---------------------------------------------------------------- TTS -> 8 kHz PCM
def piper_wav(text: str, out: Path) -> bool:
    if shutil.which("piper") is None or not agent.PIPER_MODEL.is_file():
        print("[PIPER] piper ili model nisu dostupni", flush=True)
        return False
    for flag in ("-f", "--output_file"):
        try:
            subprocess.run(["piper", "-m", str(agent.PIPER_MODEL), *agent.PIPER_EXTRA, flag, str(out)],
                           input=text + "\n", text=True, encoding="utf-8", capture_output=True,
                           timeout=30, check=True)
            if out.is_file() and out.stat().st_size > 0:
                return True
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "")[-200:]
            if "unrecognized" not in err and "unknown" not in err.lower():
                print(f"[PIPER] greska: {err}", flush=True)
        except subprocess.TimeoutExpired:
            print("[PIPER] timeout", flush=True)
            return False
    return False


def synth_wav(text, wav: Path) -> bool:
    clean = re.sub(r"[^\w\s.,!?čćžšđČĆŽŠĐ-]", "", text).strip()
    if not clean:
        return False
    wav.unlink(missing_ok=True)
    if TTS_MODE == "edge" and agent._generate_edge_tts_wav(clean, wav):
        return True
    return piper_wav(agent.latin_to_cyrillic(clean) if PIPER_CYR else clean, wav)


def wav_to_pcm8k(wav: Path) -> bytes:
    global _soxr_ok
    chains = []
    if _soxr_ok:
        chains.append(AF_BASE + ",aresample=osr=8000:resampler=soxr:precision=28")
    chains.append(AF_BASE + ",aresample=osr=8000")
    last = None
    for ch in chains:
        try:
            return subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(wav), "-af", ch,
                                   "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                                  check=True, capture_output=True, timeout=15).stdout
        except subprocess.CalledProcessError as exc:
            last = exc
            if "soxr" in ch:
                _soxr_ok = False
    raise last


def finalize(pcm: bytes, lead_ms: int, tail_ms: int) -> bytes:
    """Ujednacena glasnoca (bez clippinga), fade in/out 10 ms, tisina na pocetku i kraju."""
    pcm = pcm[: len(pcm) // 2 * 2]
    if not pcm:
        return b""
    if np is not None:
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        peak = float(np.abs(a).max()) or 1.0
        rms_v = float(np.sqrt((a * a).mean())) or 1.0
        a *= min(TARGET_RMS / rms_v, PEAK_LIMIT / peak, 8.0)
        f = min(80, len(a) // 2)
        if f:
            ramp = np.sin(np.linspace(0, np.pi / 2, f, dtype=np.float32)) ** 2
            a[:f] *= ramp
            a[-f:] *= ramp[::-1]
        body = np.clip(a, -32768, 32767).astype("<i2").tobytes()
    else:
        arr = array("h")
        arr.frombytes(pcm)
        if sys.byteorder == "big":
            arr.byteswap()
        n = len(arr)
        peak = max(max(arr), -min(arr)) or 1
        rms_v = math.sqrt(sum(x * x for x in arr) / n) or 1.0
        g = min(TARGET_RMS / rms_v, PEAK_LIMIT / peak, 8.0)
        out = array("h", (max(-32768, min(32767, int(x * g))) for x in arr))
        f = min(80, n // 2)
        for i in range(f):
            w = i / f
            out[i] = int(out[i] * w)
            out[n - 1 - i] = int(out[n - 1 - i] * w)
        if sys.byteorder == "big":
            out.byteswap()
        body = out.tobytes()
    return bytes(8 * lead_ms * 2) + body + bytes(8 * tail_ms * 2)


_cache: "OrderedDict[tuple, bytes]" = OrderedDict()


def tts_pcm(text, folder: Path, lead_ms=60, tail_ms=90) -> bytes:
    key = (TTS_MODE, PIPER_CYR, DENOISE, text, lead_ms, tail_ms)
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


def split_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    merged = []
    for p in parts:
        if merged and len(merged[-1]) < 14:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged or [text]


# ---------------------------------------------------------------- Player (20 ms sat)
class Player:
    def __init__(self, writer, disconnected):
        self.writer, self.disconnected = writer, disconnected
        self.buf = deque()
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = None

    def start(self): self.task = asyncio.create_task(self._run())

    def enqueue(self, pcm):
        for off in range(0, len(pcm), FRAME):
            self.buf.append(pcm[off:off + FRAME].ljust(FRAME, b"\x00"))
        if self.buf:
            self.idle.clear()

    async def wait_idle(self):
        w = asyncio.create_task(self.idle.wait())
        g = asyncio.create_task(self.disconnected.wait())
        try:
            await asyncio.wait({w, g}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (w, g):
                t.cancel()
            await asyncio.gather(w, g, return_exceptions=True)

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
                d = next_t - loop.time()
                if d > 0:
                    await asyncio.sleep(d)
                elif d < -0.2:
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


async def speak(player, text, folder, disconnected, calog=None):
    if calog:
        calog.said(text)
    for i, part in enumerate(split_sentences(text)):
        if disconnected.is_set():
            return
        player.enqueue(await asyncio.to_thread(tts_pcm, part, folder, 60 if i == 0 else 0, 90))
    await player.wait_idle()
    await asyncio.sleep(0.15)


# ---------------------------------------------------------------- jedna replika
_EXTRACT_TAKES_STATE = len(inspect.signature(agent.extract).parameters) >= 3
UNCLEAR = "Nisam vas najbolje čuo. Možete li da ponovite?"


def process_turn(pcm, folder, state, calendar, calog, turn_no):
    wav = folder / "input.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "8000",
                    "-ac", "1", "-i", "pipe:0", "-af", "highpass=f=100,lowpass=f=3600,dynaudnorm=f=150:g=7",
                    "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                   input=pcm, check=True, capture_output=True, timeout=10)
    if SAVE_AUDIO:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wav, LOG_DIR / datetime.now().strftime(f"heard_%Y%m%d_%H%M%S_t{turn_no}.wav"))
    raw = transcribe_sr(wav)
    text = cyr_to_lat(raw)
    if not text:
        calog.heard("(prazan transkript)")
        return UNCLEAR, False
    if not looks_serbian(text):
        calog.heard(f"{text}   [ODBACENO: nije srpski / halucinacija]")
        return UNCLEAR, False
    calog.heard(text)

    try:
        info = (agent.extract(text, state["stage"], state) if _EXTRACT_TAKES_STATE
                else agent.extract(text, state["stage"]))
    except Exception as exc:
        print(f"[LLM] greška: {type(exc).__name__}", flush=True)
        info = {"intent": "other"}

    if state["stage"] == "confirm":
        if NO_RE.search(text):
            info.update(intent="book", confirm="no")
        elif YES_RE.search(text):
            info.update(intent="book", confirm="yes")
    if state["date"] and state["time"] and not state["name"] and not agent.validate_name(info.get("name")):
        g = fallback_name(text)
        if g:
            info.update(name=g, intent="book")
    if state["name"] and not state["phone"] and not agent.normalize_phone(info.get("phone")):
        g = spoken_phone(text)
        if g:
            info.update(phone=g, intent="book")

    reply, done, transfer = agent.next_reply(info, state, text, calendar)
    if DEBUG:
        print(f"[LLM] {info}\n[STATE] {state}", flush=True)
    if transfer:
        calog.info("Agent trazi prebacivanje na coveka (u testu se poziv samo zavrsava).")
        return (reply or "Povezujem vas sa doktorom."), True
    return reply, done


# ---------------------------------------------------------------- AudioSocket + VAD
async def read_packet(reader):
    h = await reader.readexactly(3)
    return h[0], await reader.readexactly(int.from_bytes(h[1:], "big"))


def rms(frame):
    s = struct.unpack("<160h", frame)
    return math.sqrt(sum(x * x for x in s) / 160)


async def listen(queue):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 8
    pre, speech, pending = deque(maxlen=15), [], bytearray()
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
                pre.append(frame)
                voiced = voiced + 1 if loud else 0
                if voiced >= 3:
                    speech = list(pre)
                    deadline = loop.time() + 12
            else:
                speech.append(frame)
                silence = 0 if loud else silence + 1
                if silence >= END_SILENCE:
                    return b"".join(speech)
    return b""


async def warm_cache(folder, texts):
    for text in texts:
        try:
            for i, part in enumerate(split_sentences(text)):
                await asyncio.to_thread(tts_pcm, part, folder, 60 if i == 0 else 0, 90)
        except Exception as exc:
            print(f"[WARM] preskačem: {type(exc).__name__}: {exc}", flush=True)


def preflight():
    def ping(name, url):
        try:
            urllib.request.urlopen(url, timeout=3).read(1)
            print(f"[CHECK] {name:8} OK   {url}", flush=True)
        except Exception as exc:
            code = getattr(exc, "code", None)
            tag = "OK  " if code and code < 500 else "FAIL"
            print(f"[CHECK] {name:8} {tag} {url} ({type(exc).__name__})", flush=True)
    base = lambda u: u.split("/", 3)[0] + "//" + u.split("/", 3)[2]
    ping("whisper", base(agent.WHISPER_URL) + "/")
    ping("llama", base(agent.LLM_URL) + "/health")
    print(f"[CHECK] ffmpeg   {'OK' if shutil.which('ffmpeg') else 'FAIL'} | piper "
          f"{'OK' if shutil.which('piper') else 'FAIL'} | numpy {'OK' if np else 'nema (sporije, radi)'}", flush=True)


GREETING = os.getenv("AIVA_GREETING", "Dobar dan, stomatološka ordinacija Osmeh. Kako mogu da vam pomognem?")
ERROR_TEXT = "Trenutno ne mogu da obradim poziv. Doviđenja."
MISS_1 = "Nisam vas čuo. Možete li da ponovite?"
MISS_2 = "Ne čujem vas dovoljno jasno. Doviđenja."


async def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("Potreban je ffmpeg u PATH-u.")
    if TTS_MODE != "edge" and not shutil.which("piper"):
        raise SystemExit("Potreban je piper u PATH-u (ili AIVA_TTS=edge).")
    if TTS_MODE == "piper" and not agent.PIPER_MODEL.is_file():
        raise SystemExit(f"Nedostaje Piper model: {agent.PIPER_MODEL}")
    if RMS_THRESHOLD <= 0 or END_SILENCE < 1:
        raise SystemExit("Proveri podešavanja za detekciju govora.")

    preflight()
    shared_calendar = agent.CalendarManager() if LIVE_BOOKING else SimCalendar()
    with tempfile.TemporaryDirectory(prefix="aiva-start-") as tmp:
        await warm_cache(Path(tmp), [GREETING, ERROR_TEXT, MISS_1, MISS_2, UNCLEAR,
                                     "Izvinite, možete li da ponovite?", "Hvala na pozivu, doviđenja."])
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
        listening, disconnected = asyncio.Event(), asyncio.Event()
        receiver = None
        player = Player(writer, disconnected)
        calog = CallLog()

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
            st = asyncio.create_task(listen(queue))
            ht = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait({st, ht}, return_when=asyncio.FIRST_COMPLETED)
                return b"" if disconnected.is_set() else st.result()
            finally:
                listening.clear()
                for t in (st, ht):
                    t.cancel()
                await asyncio.gather(st, ht, return_exceptions=True)
                while not queue.empty():
                    queue.get_nowait()

        try:
            kind, payload = await asyncio.wait_for(read_packet(reader), 5)
            if kind != 0x01 or len(payload) != 16:
                return
            print(_c("1", "\n========== [SIP] NOVI POZIV =========="), flush=True)
            receiver = asyncio.create_task(receive())
            player.start()
            state = {"stage": "slot", "date": None, "time": None, "name": None,
                     "phone": None, "offered_dates": [], "fails": 0}
            misses = 0
            with tempfile.TemporaryDirectory(prefix="aiva-call-") as tmp:
                folder = Path(tmp)
                await speak(player, GREETING, folder, disconnected, calog)
                for turn_no in range(1, MAX_TURNS + 1):
                    if disconnected.is_set():
                        break
                    pcm = await next_utterance()
                    if disconnected.is_set():
                        break
                    if pcm:
                        misses = 0
                        if DEBUG:
                            print(f"[VAD] replika {len(pcm) / 16000:.1f} s", flush=True)
                        try:
                            reply, done = await asyncio.to_thread(
                                process_turn, pcm, folder, state, shared_calendar, calog, turn_no)
                        except Exception as exc:
                            print(f"[TURN] greška: {type(exc).__name__}: {exc}", flush=True)
                            reply, done = "Izvinite, možete li da ponovite?", False
                    else:
                        misses += 1
                        calog.info("(tišina - ništa nije čuo)")
                        done = misses >= 2
                        reply = MISS_2 if done else MISS_1
                    if disconnected.is_set():
                        break
                    await speak(player, reply, folder, disconnected, calog)
                    if done:
                        break
        except (asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        except Exception as exc:
            print(f"[SIP] Obrada nije uspela: {type(exc).__name__}: {exc}", flush=True)
            if not disconnected.is_set():
                try:
                    with tempfile.TemporaryDirectory(prefix="aiva-err-") as tmp:
                        await speak(player, ERROR_TEXT, Path(tmp), disconnected, calog)
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
            calog.close()
            busy = False
            print(_c("1", "========== [SIP] POZIV ZAVRŠEN ==========\n"), flush=True)

    server = await asyncio.start_server(handle_call, HOST, PORT)
    mode = "PRAVO zakazivanje" if LIVE_BOOKING else "realan poziv, SIMULIRANO zakazivanje"
    print(f"[SIP] Slušam na {HOST}:{PORT}; {mode}; TTS={TTS_MODE}; ASR={ASR_LANG}; "
          f"agent={agent.__name__}; transkript={'ON -> ' + str(LOG_DIR) if LOG_TEXT else 'OFF'}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
