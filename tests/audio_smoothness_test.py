"""Measures how smoothly the bot's speech plays in TeamTalk, as received by
another TeamTalk client, now that speech is sent with TT_InsertAudioBlock.

    TEAMTALK_SDK_DIR=... python tests/audio_smoothness_test.py

Needs numpy and websockets, ffmpeg, two throwaway TeamTalk servers on
localhost (never real ones) with accounts admin/adminpass1 (administrator),
STAR/starpass1 and tester/testpass1 (TEST_TT_PORTS, default 19311,19312), and
optionally the real STAR coagulator (TEST_REAL_STAR, default
ws://localhost:7774) for one real speech clip.

The bot gets its audio from a stand-in coagulator run by this test, so the
reference is known exactly:
  * Tone: 20 s of 440 Hz served at 44.1 kHz (the bot resamples it to the
    channel codec's rate). Any dropout shows as a dip, any rate mistake or
    speed change as a different frequency.
  * Stereo: 15 s, 440 Hz left and 660 Hz right, in a stereo Opus channel.
  * Beep A/B/C: 3 s at 500/700/900 Hz, queued back to back (order, no overlap).
  * Speech: real synthesized speech, compared by its loudness envelope.
A receiving client records the bot's voice stream (TT_EnableAudioBlockEvent
with TeamTalk's virtual sound device, so it plays in real time as a person
would hear it) and the test checks length, dropouts, frequency, gaps in the
stream, pacing, stopping, and two servers at once.
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
import websockets.sync.client
import websockets.sync.server

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from teamtalk_sdk import load_teamtalk_sdk  # noqa: E402

tt = load_teamtalk_sdk()

PORTS = [int(p) for p in os.environ.get("TEST_TT_PORTS", "19311,19312").split(",")]
REAL_STAR = os.environ.get("TEST_REAL_STAR", "ws://localhost:7774")
FAKE_PORT = 17774
WEB_PORT = 8777
BASE = f"http://127.0.0.1:{WEB_PORT}"
BOT_NICK = "STAR Audio Test Bot"
SERVED_RATE = 44100
BOT_PYTHON = os.environ.get("BOT_PYTHON", sys.executable)
LISTENER_BUFFER_MSEC = int(os.environ.get("TEST_LISTENER_BUFFER", "100"))

results: list[tuple[str, bool]] = []
report: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append((name, bool(ok)))
    print(f"{name}: {'PASS' if ok else 'FAIL'}{'' if ok or detail == '' else '  (' + str(detail)[-600:] + ')'}", flush=True)
    return bool(ok)


def note(text: str) -> None:
    report.append(text)
    print("  " + text, flush=True)


def wait_for(predicate, timeout: float = 30, every: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(every)
    return False


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-STAR-Request": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{method} {path} -> {e.code}: {e.read().decode()}")


def server_state(name: str) -> dict:
    return next(s for s in api("GET", "/api/state")["servers"] if s["name"] == name)


# ------------------------------------------------------------ test audio


def wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """samples: float array (n,) or (n, channels) in -1..1."""
    if samples.ndim == 1:
        samples = samples[:, None]
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def tone(freq: float, seconds: float, rate: int = SERVED_RATE, amplitude: float = 0.4) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    fade = np.minimum(1, np.minimum(t, seconds - t) / 0.01)  # 10 ms in and out, no clicks
    return amplitude * np.sin(2 * np.pi * freq * t) * fade


def real_speech() -> tuple[bytes, str] | None:
    """One long clip from the real coagulator, or None if it is not there."""
    text = ("This is a test of how smoothly speech plays in TeamTalk. The quick brown fox jumps over the lazy dog, "
            "and then it runs across the field, past the old barn, over the bridge and into the forest, "
            "where it finally rests under a tall tree until the sun goes down.")
    try:
        with websockets.sync.client.connect(REAL_STAR, max_size=None) as ws:
            ws.send(json.dumps({"user": 4}))
            voices = []
            end = time.time() + 15
            while time.time() < end and not voices:
                message = ws.recv(15)
                if isinstance(message, str) and "voices" in message:
                    voices = [v if isinstance(v, str) else v.get("name") for v in json.loads(message)["voices"]]
            for n, voice in enumerate(voices[:6]):
                ws.send(json.dumps({"user": 4, "request": f"{voice}: {text}", "id": str(n + 1)}))
                end = time.time() + 20
                while time.time() < end:
                    try:
                        message = ws.recv(max(0.1, end - time.time()))
                    except TimeoutError:
                        break
                    if isinstance(message, bytes):
                        meta_len = int.from_bytes(message[:2], "little")
                        meta = json.loads(message[2:2 + meta_len]) if message[2:3] == b"{" else {}
                        return message[2 + meta_len:], f"{voice} ({meta.get('extension', 'wav')})"
                    if isinstance(message, str) and ("error" in message or "warning" in message):
                        break
    except Exception as exc:
        note(f"The real coagulator could not be used ({exc}); the speech check is skipped.")
    return None


class FakeStar:
    """A stand-in STAR coagulator serving known audio by voice name."""

    def __init__(self, clips: dict[str, tuple[bytes, str]]):
        self.clips = clips
        self.server = websockets.sync.server.serve(self.handle, "127.0.0.1", FAKE_PORT, max_size=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, ws) -> None:
        for message in ws:
            data = json.loads(message)
            if "request" not in data:
                ws.send(json.dumps({"voices": list(self.clips)}))
                continue
            voice = data["request"].split(":", 1)[0].partition("<")[0].strip()
            audio, extension = self.clips[voice]
            meta = json.dumps({"id": data["id"], "extension": extension}).encode()
            ws.send(len(meta).to_bytes(2, "little") + meta + audio)

    def close(self) -> None:
        self.server.shutdown()


# ------------------------------------------------------------- receiver


class Receiver(tt.TeamTalk, threading.Thread):
    """A TeamTalk client that records the bot's voice stream as it plays."""

    def __init__(self, port: int, username: str, password: str, nickname: str):
        tt.TeamTalk.__init__(self)
        threading.Thread.__init__(self, daemon=True)
        self.port, self.username, self.password, self.nickname = port, username, password, nickname
        self.my_id = 0
        self.ready = threading.Event()
        self.stop_flag = False
        self.lock = threading.Lock()
        self.pending: list = []
        self.blocks: list[dict] = []
        self.recording_from = 0
        self.joined_channel = 0

    def run(self) -> None:
        self.connect(tt.ttstr("127.0.0.1"), self.port, self.port)
        while not self.stop_flag:
            with self.lock:
                actions, self.pending = self.pending, []
            for action in actions:
                action()
            self.runEventLoop(20)
        self.disconnect()

    def do(self, function, wait: bool = True):
        done = threading.Event()
        box = {}

        def run():
            try:
                box["value"] = function()
            finally:
                done.set()

        with self.lock:
            self.pending.append(run)
        if wait:
            done.wait(10)
        return box.get("value")

    def onConnectSuccess(self):
        self.initSoundOutputDevice(tt.TT_SOUNDDEVICE_ID_TEAMTALK_VIRTUAL)
        self.doLogin(tt.ttstr(self.nickname), tt.ttstr(self.username), tt.ttstr(self.password), tt.ttstr("audio test"))

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        self.doJoinChannelByID(self.getRootChannelID(), tt.ttstr(""))

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self.my_id:
            self.joined_channel = user.nChannelID
            self.ready.set()

    def onUserAudioBlock(self, nUserID, nStreamType):
        block = self.acquireUserAudioBlock(tt.StreamType.STREAMTYPE_VOICE, nUserID)
        if not block:
            return
        try:
            b = block.contents
            raw = ctypes.string_at(b.lpRawAudio, b.nSamples * b.nChannels * 2) if b.nSamples else b""
            with self.lock:
                self.blocks.append({"time": time.monotonic(), "stream": b.nStreamID, "rate": b.nSampleRate,
                                    "channels": b.nChannels, "index": b.uSampleIndex, "samples": b.nSamples,
                                    "pcm": raw})
        finally:
            self.releaseUserAudioBlock(block)

    def user_id(self, nickname: str) -> int:
        for user in self.getServerUsers():
            if str(tt.ttstr(user.szNickname)) == nickname:
                return user.nUserID
        return 0

    def record(self, nickname: str) -> bool:
        uid = self.do(lambda: self.user_id(nickname))
        if not uid:
            return False
        self.recording_from = uid

        def enable():
            # A 100 ms playout buffer (TeamTalk's default is none). TeamTalk's
            # sending clock runs a fraction slower than real time (measured
            # 99.80-99.96%), so a listener without a buffer now and then
            # plays one 40 ms frame of silence whoever is sending, the old
            # media-file streaming included. With the buffer, any gap left is
            # the bot's own doing.
            jitter = tt.JitterConfig()
            jitter.nFixedDelayMSec = LISTENER_BUFFER_MSEC
            jitter.bUseAdativeDejitter = False
            tt._SetUserJitterControl(self._tt, uid, tt.StreamType.STREAMTYPE_VOICE, jitter)
            return self.enableAudioBlockEvent(uid, tt.StreamType.STREAMTYPE_VOICE, True)

        return bool(self.do(enable))

    def take(self) -> list[dict]:
        with self.lock:
            blocks, self.blocks = self.blocks, []
        return blocks


# ------------------------------------------------------------- analysis


def assemble(blocks: list[dict]):
    """The received audio as float arrays per stream (in arrival order)."""
    streams: dict[int, dict] = {}
    for b in blocks:
        s = streams.setdefault(b["stream"], {"blocks": [], "rate": b["rate"], "channels": b["channels"]})
        s["blocks"].append(b)
    out = {}
    for sid, s in streams.items():
        pcm = b"".join(b["pcm"] for b in s["blocks"])
        audio = np.frombuffer(pcm, "<i2").astype(np.float64) / 32768
        audio = audio.reshape(-1, s["channels"])
        out[sid] = dict(s, audio=audio)
    return out


def trim(audio: np.ndarray, rate: int, threshold: float = 0.02):
    level = np.abs(audio).max(axis=1)
    loud = np.nonzero(level > threshold)[0]
    if not len(loud):
        return audio[:0], 0
    return audio[loud[0]:loud[-1] + 1], loud[0]


def window_rms(signal: np.ndarray, rate: int, msec: int = 10) -> np.ndarray:
    n = max(1, rate * msec // 1000)
    usable = len(signal) // n * n
    return np.sqrt((signal[:usable].reshape(-1, n) ** 2).mean(axis=1))


def peak_freq(signal: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    return float(np.fft.rfftfreq(len(signal), 1 / rate)[np.argmax(spectrum)])


def analyze_tone(name: str, stream: dict, seconds: float, freqs: list[float]) -> None:
    rate, channels, blocks = stream["rate"], stream["channels"], stream["blocks"]
    audio, _ = trim(stream["audio"], rate)
    got = len(audio) / rate
    note(f"{name}: received {got:.3f} s at {rate} Hz, {channels} channel(s), {len(blocks)} blocks")
    check(f"{name}: full length ({seconds} s, within 60 ms)", abs(got - seconds) < 0.06, f"{got:.3f} s")
    check(f"{name}: {len(freqs)} channel(s) as sent", channels == len(freqs), channels)
    for ch, freq in enumerate(freqs[:channels]):
        signal = audio[:, ch]
        rms = window_rms(signal, rate)[5:-5]  # skip the 10 ms fades
        median = float(np.median(rms))
        dips = int((rms < 0.6 * median).sum())
        where = np.nonzero(rms < 0.6 * median)[0]
        if len(where):
            note(f"{name} ch{ch + 1}: dips at " + ", ".join(f"{(i + 5) * 0.01:.2f} s" for i in where[:8]))
        note(f"{name} ch{ch + 1}: 10 ms windows {len(rms)}, level min/median {rms.min() / median:.2f}, dips below 60%: {dips}")
        check(f"{name} ch{ch + 1}: no dropouts (every 10 ms at least 60% of the level)", dips == 0, f"{dips} dips")
        seg = rate // 2
        found = [peak_freq(signal[i:i + seg], rate) for i in range(0, len(signal) - seg, seg)]
        worst = max(abs(f - freq) for f in found)
        note(f"{name} ch{ch + 1}: frequency {min(found):.1f}-{max(found):.1f} Hz over {len(found)} half-seconds (sent {freq} Hz)")
        check(f"{name} ch{ch + 1}: steady {freq} Hz (no speed or rate change)", worst <= 3, f"off by {worst:.1f} Hz")
    check_stream(name, blocks, rate)


def check_stream(name: str, blocks: list[dict], rate: int) -> None:
    # A sample index back at 0 in the middle means the receiver saw the
    # voice stream stop and start again (a gap long enough to end a talk spurt).
    restarts = sum(1 for i, b in enumerate(blocks) if i and b["index"] == 0)
    jumps = sum(1 for a, b in zip(blocks, blocks[1:]) if b["index"] and b["index"] != a["index"] + a["samples"])
    check(f"{name}: one continuous voice stream (no restarts or skipped samples)", restarts == 0 and jumps == 0,
          f"restarts {restarts}, skipped {jumps}")
    times = np.array([b["time"] for b in blocks])
    audio_seconds = sum(b["samples"] for b in blocks) / rate
    span = times[-1] - times[0] + blocks[-1]["samples"] / rate
    gaps = np.diff(times)
    note(f"{name}: arrived over {span:.2f} s for {audio_seconds:.2f} s of audio (ratio {span / audio_seconds:.3f}); "
         f"block interval median {np.median(gaps) * 1000:.0f} ms, max {gaps.max() * 1000:.0f} ms")
    check(f"{name}: played in real time (arrival span within 3% of the audio)", abs(span / audio_seconds - 1) < 0.03,
          f"{span:.2f} s for {audio_seconds:.2f} s")
    check(f"{name}: no long pause between blocks (max under 200 ms)", gaps.max() < 0.2, f"{gaps.max() * 1000:.0f} ms")


def decode_reference(audio: bytes, rate: int, channels: int) -> np.ndarray:
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", str(rate), "-ac",
                          str(channels), "pipe:1"], input=audio, capture_output=True, check=True).stdout
    return (np.frombuffer(pcm, "<i2").astype(np.float64) / 32768).reshape(-1, channels)


def analyze_speech(name: str, stream: dict, reference: np.ndarray) -> None:
    rate = stream["rate"]
    # Trimmed at -30 dBFS: the clip starts with half a second of near
    # silence, which Opus may or may not keep above a lower threshold.
    got, _ = trim(stream["audio"], rate, 0.03)
    ref, _ = trim(reference, rate, 0.03)
    note(f"{name}: received {len(got) / rate:.3f} s, reference {len(ref) / rate:.3f} s")
    check(f"{name}: full length (within 60 ms of the reference)", abs(len(got) - len(ref)) / rate < 0.06,
          f"{len(got) / rate:.3f} vs {len(ref) / rate:.3f}")
    a, b = window_rms(got[:, 0], rate, 20), window_rms(ref[:, 0], rate, 20)
    dump = os.environ.get("TEST_DUMP_DIR")
    if dump:
        for label, samples in (("received", got), ("reference", ref)):
            Path(dump, f"speech-{label}.wav").write_bytes(wav_bytes(samples, rate))
    best, lag = -1.0, 0
    for shift in range(-100, 101):  # up to 2 s either way
        x, y = (a[shift:], b) if shift >= 0 else (a, b[-shift:])
        n = min(len(x), len(y))
        c = float(np.corrcoef(x[:n], y[:n])[0, 1])
        if c > best:
            best, lag = c, shift
    x, y = (a[lag:], b) if lag >= 0 else (a, b[-lag:])
    n = min(len(x), len(y))
    loud = y[:n] > 10 ** (-30 / 20)
    dropouts = int((loud & (x[:n] < 10 ** (-45 / 20))).sum())
    note(f"{name}: loudness envelope correlation {best:.3f} (lag {lag * 20} ms); 20 ms windows loud in the reference but silent here: {dropouts}")
    check(f"{name}: sounds like the reference (envelope correlation at least 0.95)", best >= 0.95, f"{best:.3f}")
    check(f"{name}: no dropouts in the speech", dropouts == 0, dropouts)
    check_stream(name, stream["blocks"], rate)


def main_stream(streams: dict) -> dict:
    return max(streams.values(), key=lambda s: len(s["audio"]))


# ------------------------------------------------------------------ main


def cpu_seconds(pid: int) -> float:
    fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def main() -> int:
    clips = {
        "Tone": (wav_bytes(tone(440, 20), SERVED_RATE), "wav"),
        "Stereo": (wav_bytes(np.stack([tone(440, 15), tone(660, 15)], axis=1), SERVED_RATE), "wav"),
        "Beep A": (wav_bytes(tone(500, 3), SERVED_RATE), "wav"),
        "Beep B": (wav_bytes(tone(700, 3), SERVED_RATE), "wav"),
        "Beep C": (wav_bytes(tone(900, 3), SERVED_RATE), "wav"),
    }
    speech = real_speech()
    if speech:
        clips["Speech"] = (speech[0], speech[1].rsplit("(", 1)[1].rstrip(")"))
        note(f"Real speech from the coagulator: {speech[1]}, {len(speech[0])} bytes")
    star = FakeStar(clips)

    work = Path(tempfile.mkdtemp(prefix="star-audio-test-"))
    config = work / "config.ini"
    servers = "".join(
        f"\n[server:t{i + 1}]\nhost = 127.0.0.1\ntcp_port = {port}\nudp_port = {port}\nnickname = {BOT_NICK}\n"
        f"username = STAR\npassword = starpass1\nchannel = /\n" for i, port in enumerate(PORTS))
    config.write_text(f"[star]\nhosts = ws://127.0.0.1:{FAKE_PORT}\n\n[bot]\ncommand_prefix = /\n\n[teamtalk]\n"
                      f"reconnect_seconds = 2\n\n[web]\nenabled = yes\nport = {WEB_PORT}\n" + servers)
    log_file = open(work / "bot.log", "w")
    bot = subprocess.Popen([BOT_PYTHON, str(ROOT / "bot.py"), "--config", str(config)], stdout=log_file,
                           stderr=subprocess.STDOUT, cwd=ROOT)
    receivers: list[Receiver] = []
    try:
        check("the bot joins both servers", wait_for(lambda: all(
            s["state"] == "connected" for s in api("GET", "/api/state")["servers"]), 30))
        r1 = Receiver(PORTS[0], "tester", "testpass1", "Listener 1")
        r2 = Receiver(PORTS[1], "tester", "testpass1", "Listener 2")
        receivers += [r1, r2]
        for r in receivers:
            r.start()
        check("both listeners join", r1.ready.wait(15) and r2.ready.wait(15))
        check("both listeners record the bot's voice stream", wait_for(lambda: r1.record(BOT_NICK) and r2.record(BOT_NICK), 15))
        codec = r1.do(lambda: r1.getChannel(r1.joined_channel).audiocodec)
        note(f"Root channel codec on t1: {codec.nCodec} (3 = Opus), {codec.opus.nSampleRate} Hz, "
             f"{codec.opus.nChannels} ch, {codec.opus.nBitRate} bit/s, tx {codec.opus.nTxIntervalMSec} ms")

        def speak(server: str, voice: str) -> None:
            api("POST", f"/api/servers/{server}/speak", {"voice": voice, "text": "test"})

        def finished(server: str) -> bool:
            s = server_state(server)
            return not s["speaking"] and not s["queue"]

        def play(receiver: Receiver, server: str, voice: str, seconds: float) -> dict:
            receiver.take()
            speak(server, voice)
            wait_for(lambda: server_state(server)["speaking"], 20, 0.05)
            wait_for(lambda: finished(server), seconds + 20, 0.2)
            time.sleep(1.5)  # the receiver's playout ends after the sender's
            return assemble(receiver.take())

        # A. 20 s tone, with the bot's CPU use.
        cpu0, t0 = cpu_seconds(bot.pid), time.time()
        streams = play(r1, "t1", "Tone", 20)
        cpu = (cpu_seconds(bot.pid) - cpu0) / (time.time() - t0) * 100
        if check("Tone: received", streams):
            analyze_tone("Tone", main_stream(streams), 20, [440])
        note(f"Bot CPU while speaking: {cpu:.1f}% of one core")

        # B. Real speech.
        if speech:
            streams = play(r1, "t1", "Speech", 30)
            if check("Speech: received", streams):
                s = main_stream(streams)
                analyze_speech("Speech", s, decode_reference(speech[0], s["rate"], s["channels"]))

        # C. Three queued back to back.
        r1.take()
        for voice in ("Beep A", "Beep B", "Beep C"):
            speak("t1", voice)
        wait_for(lambda: server_state("t1")["speaking"], 20, 0.05)
        wait_for(lambda: finished("t1"), 40)
        time.sleep(1.5)
        streams = assemble(r1.take())
        ordered = sorted(streams.values(), key=lambda s: s["blocks"][0]["time"])
        ordered = [s for s in ordered if len(trim(s["audio"], s["rate"])[0]) > s["rate"]]
        check("Queue: three separate streams", len(ordered) == 3, len(ordered))
        if len(ordered) == 3:
            freqs = [peak_freq(trim(s["audio"], s["rate"])[0][:, 0], s["rate"]) for s in ordered]
            note("Queue: frequencies in order " + ", ".join(f"{f:.0f} Hz" for f in freqs))
            check("Queue: in the order sent (500, 700, 900 Hz)", all(abs(f - e) < 5 for f, e in zip(freqs, (500, 700, 900))), freqs)
            for label, s in zip("ABC", ordered):
                analyze_tone(f"Beep {label}", s, 3, [{"A": 500, "B": 700, "C": 900}[label]])
            first_end = ordered[0]["blocks"][-1]["time"]
            second_start = ordered[1]["blocks"][0]["time"]
            check("Queue: no overlap (each starts after the last ends)", second_start >= first_end - 0.05,
                  f"{second_start - first_end:.3f} s")
            note(f"Queue: pause between messages {second_start - first_end:.2f} s")

        # D. Stop in the middle.
        r1.take()
        speak("t1", "Tone")
        wait_for(lambda: server_state("t1")["speaking"], 20, 0.05)
        time.sleep(3)
        stop_at = time.monotonic()
        api("POST", "/api/servers/t1/stop")
        time.sleep(3)
        blocks = r1.take()
        loud = [b for b in blocks if b["pcm"] and np.abs(np.frombuffer(b["pcm"], "<i2")).max() > 1000]
        latency = loud[-1]["time"] - stop_at if loud else 99
        note(f"Stop: last sound {latency:.2f} s after the stop request")
        check("Stop: the speech stops within 1.2 s", latency < 1.2, f"{latency:.2f} s")
        check("Stop: nothing left speaking", finished("t1"))
        streams = play(r1, "t1", "Beep A", 3)
        check("Stop: the next message plays normally after a stop", streams and abs(
            len(trim(main_stream(streams)["audio"], main_stream(streams)["rate"])[0]) / main_stream(streams)["rate"] - 3) < 0.06)

        # E. Two servers at once.
        r1.take(), r2.take()
        speak("t1", "Tone")
        speak("t2", "Tone")
        wait_for(lambda: server_state("t1")["speaking"] and server_state("t2")["speaking"], 20, 0.05)
        wait_for(lambda: finished("t1") and finished("t2"), 45)
        time.sleep(1.5)
        for label, r in (("Both servers t1", r1), ("Both servers t2", r2)):
            streams = assemble(r.take())
            if check(f"{label}: received", streams):
                analyze_tone(label, main_stream(streams), 20, [440])

        # F. A stereo Opus channel.
        admin = Receiver(PORTS[0], "admin", "adminpass1", "Admin")
        receivers.append(admin)
        admin.start()
        admin.ready.wait(15)

        def make_stereo():
            channel = tt.Channel()
            channel.nParentID = admin.getRootChannelID()
            channel.szName = tt.ttstr("Stereo")
            channel.nMaxUsers = 100
            channel.audiocodec.nCodec = tt.Codec.OPUS_CODEC
            o = channel.audiocodec.opus
            o.nSampleRate, o.nChannels, o.nApplication, o.nComplexity = 48000, 2, 2049, 10
            o.bFEC, o.bDTX, o.nBitRate, o.bVBR, o.bVBRConstraint = True, False, 128000, True, False
            o.nTxIntervalMSec, o.nFrameSizeMSec = 40, 0
            return admin.doMakeChannel(channel)

        admin.do(make_stereo)
        check("Stereo: channel made", wait_for(lambda: admin.do(lambda: admin.getChannelIDFromPath(tt.ttstr("/Stereo/"))) > 0, 10))
        cfg = api("GET", "/api/servers/t1")["config"]
        api("PUT", "/api/servers/t1", {"config": dict(cfg, channel="/Stereo/")})
        stereo_id = admin.do(lambda: admin.getChannelIDFromPath(tt.ttstr("/Stereo/")))
        r1.do(lambda: r1.doJoinChannelByID(stereo_id, tt.ttstr("")))
        check("Stereo: the bot and the listener are in it", wait_for(
            lambda: server_state("t1")["channel_path"] == "/Stereo/" and r1.do(lambda: r1.getMyChannelID()) == stereo_id, 30))
        wait_for(lambda: r1.record(BOT_NICK), 10)
        streams = play(r1, "t1", "Stereo", 15)
        if check("Stereo: received", streams):
            analyze_tone("Stereo", main_stream(streams), 15, [440, 660])
        log_text = (work / "bot.log").read_text()
        check("no decoding or speaking errors in the bot's log", "Could not" not in log_text and "Traceback" not in log_text,
              [line for line in log_text.splitlines() if "Could not" in line or "Traceback" in line][-5:])
    except Exception:
        import traceback
        check("the test ran to the end", False, traceback.format_exc())
    finally:
        for r in receivers:
            r.stop_flag = True
        bot.send_signal(signal.SIGINT)
        try:
            bot.wait(15)
        except subprocess.TimeoutExpired:
            bot.kill()
        star.close()
        log_file.close()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    for n in failed:
        print("FAILED:", n)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
