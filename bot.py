#!/usr/bin/env python3
"""STAR TeamTalk bot.

This is a headless STAR user client that listens for private TeamTalk
messages, synthesizes text through a STAR coagulator, and streams each
generated audio file into the bot's current TeamTalk channel.
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import websockets.exceptions
import websockets.sync.client

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from teamtalk_sdk import load_teamtalk_sdk  # noqa: E402
from botlog import log  # noqa: E402

# Run as a script, this file is __main__; web.py imports it as "bot", which
# must be this same module (not a second copy with its own SDK and classes).
if __name__ == "__main__":
    sys.modules.setdefault("bot", sys.modules[__name__])

tt = load_teamtalk_sdk()


USER_REVISION = 4
DEFAULT_CONFIG = SCRIPT_DIR / "config.ini"
EXIT_RESTART = 75
# What the bot reports as its TeamTalk client (not configurable).
CLIENT_NAME = "STAR TeamTalk Bot"

# Speech is sent as the bot's voice stream with TT_InsertAudioBlock: decoded
# to 16-bit PCM in the channel codec's format and given to TeamTalk in pieces
# of AUDIO_CHUNK_MSEC. TeamTalk sends it in real time and reports how much it
# has sent (uElapsedMSec of CLIENTEVENT_AUDIOINPUT, about every 40 ms); the
# bot keeps AUDIO_AHEAD_MSEC more than that inserted. (uQueueMSec is only
# TeamTalk's current frame, not this backlog.) TeamTalk cannot drop what it
# has queued, so the backlog is what still plays after Stop; it holds about
# 3 s at most.
AUDIO_CHUNK_MSEC = 100
AUDIO_AHEAD_MSEC = 500
# Silence after each message: the codec keeps a little of what it last
# encoded, and without this it came out as a faint blip (about -40 dB) at the
# start of the next message, and the last few milliseconds were held back.
SPEECH_TAIL_MSEC = 100
# Longest speech kept for one message (at 48 kHz stereo about 92 MB).
MAX_SPEECH_SECONDS = 480


@dataclass
class Speech:
    """One decoded message waiting to be spoken."""

    pcm: bytes
    sample_rate: int
    channels: int
    requester_id: int
    description: str

    @property
    def frame_bytes(self) -> int:
        return 2 * self.channels

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.frame_bytes / self.sample_rate


def decode_audio(audio: bytes, extension: str, sample_rate: int, channels: int, temp_dir: str = "") -> bytes:
    """Decodes a synthesized file (wav, mp3, ogg...) to 16-bit little-endian
    PCM with ffmpeg. Reads it from a pipe; formats that need seeking are
    written to a temporary file first."""
    output = ["-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels),
              "-t", str(MAX_SPEECH_SECONDS), "pipe:1"]
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    result = subprocess.run(base + ["-i", "pipe:0"] + output, input=audio, capture_output=True, timeout=60)
    if result.returncode == 0 and result.stdout:
        return result.stdout
    extension = "".join(char for char in extension.lower() if char.isalnum()) or "wav"
    if temp_dir:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="star_tt_", suffix=f".{extension}", dir=temp_dir or None) as fp:
        fp.write(audio)
        fp.flush()
        result = subprocess.run(base + ["-i", fp.name] + output, capture_output=True, timeout=60)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError((result.stderr.decode(errors="replace").strip() or "no audio").splitlines()[-1])
    return result.stdout


def connect_star_websocket(uri: str):
    kwargs = {}
    parameters = inspect.signature(websockets.sync.client.connect).parameters
    if "max_size" in parameters:
        kwargs["max_size"] = None
    if "max_queue" in parameters:
        kwargs["max_queue"] = 4096
    return websockets.sync.client.connect(uri, **kwargs)


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "yes", "y", "true", "on"}


def parse_list(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = []
        for line in value.replace(",", "\n").splitlines():
            items.append(line.strip())
    return [item for item in items if item]


def normalize_voices(raw_voices) -> list[str]:
    voices = []
    for voice in raw_voices or []:
        if isinstance(voice, dict):
            name = voice.get("name") or voice.get("label") or voice.get("voice")
        else:
            name = str(voice)
        if name:
            voices.append(name)
    return voices


def normalize_request_id(raw_id) -> str:
    raw_id = str(raw_id)
    parts = raw_id.split("_")
    if len(parts) >= 3:
        return parts[1]
    return raw_id


def prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    return value if value else default


def prompt_bool(message: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{message} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "true", "1", "on"}:
            return True
        if value in {"n", "no", "false", "0", "off"}:
            return False
        print("Please enter yes or no.")


def prompt_int(message: str, default: int, minimum: int | None = None) -> int:
    while True:
        raw = prompt(message, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a number.")
            continue
        if minimum is not None and value < minimum:
            print(f"Please enter {minimum} or greater.")
            continue
        return value


def prompt_list(message: str, defaults: list[str]) -> list[str]:
    print(message)
    print("Enter one value per line. Leave a blank line when finished.")
    values: list[str] = []
    for default in defaults:
        value = prompt("Value", default)
        if value:
            values.append(value)
    while True:
        value = input("Value: ").strip()
        if not value:
            break
        values.append(value)
    return values


def write_multiline(values: list[str]) -> str:
    if not values:
        return ""
    return "\n" + "\n".join(values)


@dataclass
class StarSettings:
    hosts: list[str] = field(default_factory=lambda: ["ws://localhost:7774"])
    request_timeout: float = 60.0


@dataclass
class BotSettings:
    command_prefix: str = "!"
    default_voice: str = ""
    voice_preview_text: str = "Hello there, my name is {voice}."
    max_voice_results: int = 20
    temp_dir: str = ""


@dataclass
class TeamTalkGlobalSettings:
    reconnect_seconds: float = 5.0
    license_name: str = ""
    license_key: str = ""


@dataclass
class TeamTalkServerSettings:
    name: str
    host: str
    tcp_port: int = 10333
    udp_port: int = 10333
    encrypted: bool = False
    nickname: str = "STAR Bot"
    username: str = ""
    password: str = ""
    channel: str = "/"
    channel_password: str = ""
    enabled: bool = True


@dataclass
class WebSettings:
    # The management web interface (web.py). It has no password: keep it on
    # 127.0.0.1 behind a proxy that admits only trusted addresses.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8776


@dataclass
class AppConfig:
    star: StarSettings
    bot: BotSettings
    teamtalk: TeamTalkGlobalSettings
    servers: list[TeamTalkServerSettings]
    web: WebSettings = field(default_factory=WebSettings)


SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def read_config(path: Path) -> AppConfig:
    parser = configparser.ConfigParser()
    parser.read(path)

    star_section = parser["star"] if parser.has_section("star") else {}
    bot_section = parser["bot"] if parser.has_section("bot") else {}
    tt_section = parser["teamtalk"] if parser.has_section("teamtalk") else {}
    web_section = parser["web"] if parser.has_section("web") else {}

    star_hosts = parse_list(star_section.get("hosts", "ws://localhost:7774"))
    star = StarSettings(
        hosts=star_hosts or ["ws://localhost:7774"],
        request_timeout=float(star_section.get("request_timeout", "60")),
    )
    bot = BotSettings(
        command_prefix=bot_section.get("command_prefix", "!") or "!",
        default_voice=bot_section.get("default_voice", ""),
        voice_preview_text=bot_section.get(
            "voice_preview_text", "Hello there, my name is {voice}."
        ),
        max_voice_results=int(bot_section.get("max_voice_results", "20")),
        temp_dir=bot_section.get("temp_dir", ""),
    )
    teamtalk = TeamTalkGlobalSettings(
        reconnect_seconds=float(tt_section.get("reconnect_seconds", "5")),
        license_name=tt_section.get("license_name", ""),
        license_key=tt_section.get("license_key", ""),
    )

    servers = []
    for section_name in parser.sections():
        if not section_name.startswith("server:"):
            continue
        section = parser[section_name]
        name = section_name.split(":", 1)[1].strip() or f"server{len(servers) + 1}"
        host = section.get("host", "").strip()
        if not host:
            continue
        servers.append(
            TeamTalkServerSettings(
                name=name,
                host=host,
                tcp_port=int(section.get("tcp_port", "10333")),
                udp_port=int(section.get("udp_port", section.get("tcp_port", "10333"))),
                encrypted=parse_bool(section.get("encrypted"), False),
                nickname=section.get("nickname", "STAR Bot"),
                username=section.get("username", ""),
                password=section.get("password", ""),
                channel=section.get("channel", "/"),
                channel_password=section.get("channel_password", ""),
                enabled=parse_bool(section.get("enabled"), True),
            )
        )

    web = WebSettings(
        enabled=parse_bool(web_section.get("enabled"), False),
        host=web_section.get("host", "127.0.0.1") or "127.0.0.1",
        port=int(web_section.get("port", "8776")),
    )
    return AppConfig(star=star, bot=bot, teamtalk=teamtalk, servers=servers, web=web)


def write_config(path: Path, config: AppConfig) -> None:
    parser = configparser.ConfigParser()
    parser["star"] = {
        "hosts": write_multiline(config.star.hosts),
        "request_timeout": str(config.star.request_timeout),
    }
    parser["bot"] = {
        "command_prefix": config.bot.command_prefix,
        "default_voice": config.bot.default_voice,
        "voice_preview_text": config.bot.voice_preview_text,
        "max_voice_results": str(config.bot.max_voice_results),
        "temp_dir": config.bot.temp_dir,
    }
    parser["teamtalk"] = {
        "reconnect_seconds": str(config.teamtalk.reconnect_seconds),
        "license_name": config.teamtalk.license_name,
        "license_key": config.teamtalk.license_key,
    }
    parser["web"] = {
        "enabled": "yes" if config.web.enabled else "no",
        "host": config.web.host,
        "port": str(config.web.port),
    }
    for server in config.servers:
        parser[f"server:{server.name}"] = {
            "enabled": "yes" if server.enabled else "no",
            "host": server.host,
            "tcp_port": str(server.tcp_port),
            "udp_port": str(server.udp_port),
            "encrypted": "yes" if server.encrypted else "no",
            "nickname": server.nickname,
            "username": server.username,
            "password": server.password,
            "channel": server.channel,
            "channel_password": server.channel_password,
        }

    # Written beside the old file and moved over it, so a crash never leaves
    # half a file; readable by its owner only (it holds passwords).
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        parser.write(fp)
        fp.flush()
        os.fsync(fp.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def configure(path: Path) -> None:
    existing = read_config(path) if path.exists() else AppConfig(
        star=StarSettings(),
        bot=BotSettings(),
        teamtalk=TeamTalkGlobalSettings(),
        servers=[],
    )

    print("STAR TeamTalk Bot configuration")
    print(f"Writing {path}")
    star_hosts = prompt_list("STAR coagulator websocket hosts", existing.star.hosts)
    star = StarSettings(
        hosts=star_hosts or ["ws://localhost:7774"],
        request_timeout=prompt_int(
            "Seconds to wait for a synthesis response",
            int(existing.star.request_timeout),
            minimum=1,
        ),
    )
    bot = BotSettings(
        command_prefix=prompt("Private-message command prefix", existing.bot.command_prefix),
        default_voice=prompt("Default voice for users without a selection", existing.bot.default_voice),
        voice_preview_text=prompt("Voice preview text", existing.bot.voice_preview_text),
        max_voice_results=prompt_int("Maximum voices to list in one reply", existing.bot.max_voice_results, minimum=1),
        temp_dir=prompt("Temp directory for synthesized audio that must be decoded from a file", existing.bot.temp_dir),
    )
    teamtalk = TeamTalkGlobalSettings(
        reconnect_seconds=prompt_int("Reconnect delay in seconds", int(existing.teamtalk.reconnect_seconds), minimum=1),
        license_name=prompt("TeamTalk SDK license name, if required", existing.teamtalk.license_name),
        license_key=prompt("TeamTalk SDK license key, if required", existing.teamtalk.license_key),
    )

    servers: list[TeamTalkServerSettings] = []
    default_count = len(existing.servers) or 1
    count = prompt_int("How many TeamTalk servers should the bot connect to", default_count, minimum=1)
    for index in range(count):
        old = existing.servers[index] if index < len(existing.servers) else TeamTalkServerSettings(name=f"server{index + 1}", host="")
        print(f"\nTeamTalk server {index + 1}")
        name = prompt("Config section name", old.name or f"server{index + 1}")
        host = prompt("Host address", old.host)
        tcp_port = prompt_int("TCP port", old.tcp_port, minimum=1)
        udp_port = prompt_int("UDP port", old.udp_port, minimum=1)
        servers.append(
            TeamTalkServerSettings(
                name=name,
                host=host,
                tcp_port=tcp_port,
                udp_port=udp_port,
                encrypted=prompt_bool("Encrypted connection", old.encrypted),
                nickname=prompt("Bot nickname", old.nickname),
                username=prompt("Username", old.username),
                password=prompt("Password", old.password),
                channel=prompt("Channel path to join", old.channel),
                channel_password=prompt("Channel password", old.channel_password),
                enabled=prompt_bool("Enable this server", old.enabled),
            )
        )

    write_config(path, AppConfig(star=star, bot=bot, teamtalk=teamtalk, servers=servers, web=existing.web))
    print(f"Configuration written to {path}")


class StarConnection(threading.Thread):
    def __init__(self, name: str, host: str, request_timeout: float):
        super().__init__(name=f"star:{name}", daemon=True)
        self.display_name = name
        self.host = host
        self.request_timeout = request_timeout
        self.stop_event = threading.Event()
        self.outbox: queue.Queue[tuple[str, dict | None]] = queue.Queue()
        self.pending: dict[str, tuple[float, Callable, str]] = {}
        self.voices: list[str] = []
        self.connected = False
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()

    def has_voice(self, voice: str) -> bool:
        voice_lower = voice.lower()
        with self._lock:
            return any(v.lower() == voice_lower for v in self.voices)

    def get_voices(self) -> list[str]:
        with self._lock:
            return list(self.voices)

    def request(self, voice: str, text: str, callback: Callable[[str | None, str | None, bytes | None], None]) -> bool:
        if not self.connected:
            return False
        request_id = str(next(self._ids))
        self.pending[request_id] = (time.time(), callback, f"{voice}: {text}")
        self.outbox.put(
            (
                request_id,
                {"user": USER_REVISION, "request": f"{voice}: {text}", "id": request_id},
            )
        )
        return True

    def run(self) -> None:
        last_exception = ""
        while not self.stop_event.is_set():
            try:
                with connect_star_websocket(self.host) as websocket:
                    self.connected = True
                    websocket.send(json.dumps({"user": USER_REVISION}))
                    log(f"Connected to STAR coagulator {self.display_name}: {self.host}")
                    while not self.stop_event.is_set():
                        self._drain_outbox(websocket)
                        self._expire_pending()
                        try:
                            message = websocket.recv(0.25)
                        except TimeoutError:
                            continue
                        if isinstance(message, bytes):
                            self._handle_binary(message)
                        else:
                            self._handle_message(message)
            except websockets.exceptions.ConnectionClosedOK:
                pass
            except Exception as exc:
                message = str(exc)
                if message != last_exception:
                    log(f"STAR coagulator {self.display_name} ({self.host}) is not reachable, trying again: {exc}", level="WARNING")
                last_exception = message
            finally:
                self.connected = False
                with self._lock:
                    self.voices = []
                self._fail_all("STAR connection closed")

            if not self.stop_event.wait(3):
                continue

    def _drain_outbox(self, websocket) -> None:
        while True:
            try:
                _, payload = self.outbox.get_nowait()
            except queue.Empty:
                return
            if payload is not None:
                websocket.send(json.dumps(payload))

    def _expire_pending(self) -> None:
        now = time.time()
        expired = [
            request_id
            for request_id, (created, _, _) in self.pending.items()
            if now - created > self.request_timeout
        ]
        for request_id in expired:
            _, callback, _ = self.pending.pop(request_id)
            callback(f"Synthesis request {request_id} timed out", None, None)

    def _fail_all(self, reason: str) -> None:
        pending = list(self.pending.values())
        self.pending.clear()
        for _, callback, _ in pending:
            callback(reason, None, None)

    def _handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log(f"Invalid message from STAR coagulator {self.display_name}: {message}", level="WARNING")
            return
        if "voices" in payload:
            with self._lock:
                self.voices = normalize_voices(payload["voices"])
            log(f"STAR coagulator {self.display_name} has {len(self.voices)} voices")
        elif "status" in payload or "error" in payload or "warning" in payload:
            request_id = normalize_request_id(payload.get("id", ""))
            text = payload.get("status") or payload.get("error") or payload.get("warning")
            if request_id and request_id in self.pending:
                _, callback, _ = self.pending.pop(request_id)
                callback(text, None, None)
            else:
                log(f"STAR coagulator {self.display_name}: {text}")

    def _handle_binary(self, message: bytes) -> None:
        if len(message) < 3:
            return
        meta_len = int.from_bytes(message[:2], "little")
        if len(message) < meta_len + 2:
            return
        raw_meta = message[2 : meta_len + 2].decode(errors="replace")
        if raw_meta.startswith("{"):
            try:
                meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                return
        else:
            meta = {"id": raw_meta}
        request_id = normalize_request_id(meta.get("id", ""))
        if request_id not in self.pending:
            return
        _, callback, _ = self.pending.pop(request_id)
        callback(None, meta.get("extension", "wav"), message[meta_len + 2 :])


class StarPool:
    def __init__(self, settings: StarSettings):
        self.connections = [
            StarConnection(str(index + 1), host, settings.request_timeout)
            for index, host in enumerate(settings.hosts)
        ]

    def start(self) -> None:
        for connection in self.connections:
            connection.start()

    def stop(self) -> None:
        for connection in self.connections:
            connection.stop()

    def join(self, timeout: float | None = None) -> None:
        for connection in self.connections:
            connection.join(timeout=timeout)

    def all_voices(self) -> list[str]:
        seen = set()
        voices = []
        for connection in self.connections:
            for voice in connection.get_voices():
                key = voice.lower()
                if key not in seen:
                    seen.add(key)
                    voices.append(voice)
        voices.sort(key=str.lower)
        return voices

    def connected_count(self) -> int:
        return sum(1 for connection in self.connections if connection.connected)

    def status(self) -> list[dict]:
        return [
            {
                "name": connection.display_name,
                "host": connection.host,
                "connected": connection.connected,
                "voices": len(connection.get_voices()),
                "pending": len(connection.pending),
            }
            for connection in self.connections
        ]

    def request(self, voice: str, text: str, callback: Callable[[str | None, str | None, bytes | None], None]) -> bool:
        lookup_voice = voice.partition("<")[0].strip()
        candidates = [connection for connection in self.connections if connection.has_voice(lookup_voice)]
        if not candidates:
            candidates = [connection for connection in self.connections if connection.connected]
        for connection in candidates:
            if connection.request(voice, text, callback):
                return True
        return False


class TeamTalkBotConnection(tt.TeamTalk, threading.Thread):
    def __init__(
        self,
        server: TeamTalkServerSettings,
        global_settings: TeamTalkGlobalSettings,
        bot_settings: BotSettings,
        star_pool: StarPool,
        manager: "BotManager | None" = None,
    ):
        tt.TeamTalk.__init__(self)
        threading.Thread.__init__(self, name=f"teamtalk:{server.name}", daemon=True)
        self.server = server
        self.global_settings = global_settings
        self.bot_settings = bot_settings
        self.star_pool = star_pool
        self.manager = manager
        # What the connection is doing, for the web page: connecting,
        # logging_in, joining, connected or reconnecting.
        self.state = "connecting"
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self.user_voices: dict[str, str] = {}
        self.user_rates: dict[str, str] = {}
        self.user_pitches: dict[str, str] = {}
        self.users_announcing_names: set[str] = set()
        self.user_last_results: dict[str, list[str]] = {}
        self.text_parts: dict[tuple[int, int, int], list[tt.TextMessage]] = {}
        self.media_queue: queue.Queue[Speech] = queue.Queue()
        self.current_media: Speech | None = None
        # The audio input session of current_media (TT_InsertAudioBlock).
        self._stream_ids = itertools.count(1)
        self._stream_id = 0
        self._sent_bytes = 0
        self._flushed = False
        self._elapsed_msec = 0
        self._speech_deadline = 0.0
        # Decoded one at a time, in the order the speech arrived.
        self._decode_queue: queue.Queue = queue.Queue()
        self._decoder: threading.Thread | None = None
        # The channel codec's sample rate and channels (speech is decoded to it).
        self.audio_format = (48000, 1)
        self.pending_channel_join = False
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0
        self.reconnect_now = False
        self._my_user_id = 0

    def stop(self) -> None:
        self.stop_event.set()
        self.actions.put(lambda: self._shutdown_streaming())

    def _log(self, message: str, level: str = "INFO") -> None:
        log(message, server=self.server.name, level=level)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._reset_runtime()
                self.state = "connecting"
                self._log(f"Connecting to {self.server.host}:{self.server.tcp_port}")
                ok = self.connect(
                    tt.ttstr(self.server.host),
                    self.server.tcp_port,
                    self.server.udp_port,
                    bEncrypted=self.server.encrypted,
                )
                if not ok:
                    self._log("The connection could not be started", "WARNING")
                    self.state = "reconnecting"
                    self.stop_event.wait(self.global_settings.reconnect_seconds)
                    continue
                while not self.stop_event.is_set() and not self.reconnect_now:
                    self._drain_actions()
                    self.runEventLoop(100)
                    self._check_speech_deadline()
                self._shutdown_streaming()
                self.disconnect()
            except Exception:
                self._log(traceback.format_exc().rstrip(), "ERROR")
            if not self.stop_event.is_set():
                self.state = "reconnecting"
                self.stop_event.wait(self.global_settings.reconnect_seconds)
        # Whatever the web page or a command was waiting for is not coming.
        self._drain_actions()

    def call(self, function: Callable[[], object], timeout: float = 5.0):
        """Runs `function` on this connection's own thread (where every
        TeamTalk call and every per-user setting lives) and returns its
        result. For the web interface."""
        if threading.current_thread() is self:
            return function()
        if not self.is_alive() or self.stop_event.is_set():
            raise RuntimeError("the bot is not connected to this server")
        done = threading.Event()
        result: dict = {}

        def run() -> None:
            try:
                result["value"] = function()
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        self.actions.put(run)
        if not done.wait(timeout):
            raise TimeoutError("the connection did not answer in time")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def snapshot(self) -> dict:
        """What the web page shows about this server (on the connection's
        thread, through `call`)."""
        info = {
            "channel_path": "",
            "others": [],
            "queue": self.media_queue.qsize(),
            "speaking": self.current_media.description if self.current_media else "",
            "can_speak": None,
        }
        if self.logged_in:
            # Speech is the bot's voice stream: its account needs the right
            # to transmit voice (administrators have every right).
            account = self.getMyUserAccount()
            info["can_speak"] = bool(account.uUserType & tt.UserType.USERTYPE_ADMIN
                                     or account.uUserRights & tt.UserRight.USERRIGHT_TRANSMIT_VOICE)
        if self.logged_in and self.current_channel_id > 0:
            info["channel_path"] = str(tt.ttstr(self.getChannelPath(self.current_channel_id)) or "")
            names = []
            for user in self.getChannelUsers(self.current_channel_id):
                if user.nUserID == self._my_user_id:
                    continue
                nickname = str(tt.ttstr(user.szNickname) or "").strip()
                username = str(tt.ttstr(user.szUsername) or "").strip()
                names.append(nickname or username or f"user {user.nUserID}")
            info["others"] = sorted(names, key=str.lower)
        return info

    def people(self) -> list[dict]:
        """Each person's choices on this server (voice, rate, pitch, names)."""
        keys = set(self.user_voices) | set(self.user_rates) | set(self.user_pitches) | self.users_announcing_names
        return [
            {
                "key": key,
                "voice": self.user_voices.get(key, ""),
                "rate": self.user_rates.get(key, ""),
                "pitch": self.user_pitches.get(key, ""),
                "names": key in self.users_announcing_names,
            }
            for key in sorted(keys, key=str.lower)
        ]

    def forget_person(self, key: str) -> bool:
        found = any(
            key in table
            for table in (self.user_voices, self.user_rates, self.user_pitches, self.users_announcing_names)
        )
        self.user_voices.pop(key, None)
        self.user_rates.pop(key, None)
        self.user_pitches.pop(key, None)
        self.users_announcing_names.discard(key)
        self.user_last_results.pop(key, None)
        return found

    def stop_speaking(self) -> int:
        """Stops the current stream and clears the queue; returns how many
        streams were dropped."""
        count = self.media_queue.qsize() + (1 if self.current_media else 0)
        self._shutdown_streaming()
        return count

    def speak_from_web(self, voice: str, text: str) -> str:
        """Speaks `text` in the bot's channel with `voice` (the web page's
        Say something). Runs on the connection's thread."""
        if not self.in_channel:
            raise RuntimeError("the bot is not in a channel on this server yet")
        description = f"{voice} (from the web page)"

        def done(error: str | None, extension: str | None, audio: bytes | None) -> None:
            if error or not audio:
                self._log(f"Synthesis for the web page failed: {error or 'empty audio response'}", "WARNING")
                return
            self._prepare_speech(0, description, extension or "wav", audio)

        if not self.star_pool.request(voice, text, done):
            raise RuntimeError("no STAR coagulator is connected")
        return f"Synthesizing with {voice}. It plays in {self.server.channel or 'the channel'} when it is ready."

    def _reset_runtime(self) -> None:
        self.reconnect_now = False
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0
        self.pending_channel_join = False
        self.text_parts.clear()
        self.current_media = None
        self._stream_id = 0
        self._clear_queue()

    def _drain_actions(self) -> None:
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                return
            action()

    def _clear_queue(self) -> None:
        while not self.media_queue.empty():
            try:
                self.media_queue.get_nowait()
            except queue.Empty:
                break

    def _shutdown_streaming(self) -> None:
        if self.current_media is not None:
            # NULL ends the audio input session at once (queued audio is dropped).
            try:
                self.insertAudioBlock(None)
            except Exception:
                pass
            self.current_media = None
            self._stream_id = 0
        self._clear_queue()

    def _user_key(self, user_id: int) -> str:
        user = self.getUser(user_id)
        username = str(tt.ttstr(user.szUsername) or "").strip()
        nickname = str(tt.ttstr(user.szNickname) or "").strip()
        return username or nickname or str(user_id)

    def _user_display_name(self, user_id: int) -> str:
        user = self.getUser(user_id)
        nickname = str(tt.ttstr(user.szNickname) or "").strip()
        username = str(tt.ttstr(user.szUsername) or "").strip()
        return nickname or username or str(user_id)

    def _user_in_bot_channel(self, user_id: int) -> bool:
        bot_channel_id = self.current_channel_id or self.getMyChannelID()
        if bot_channel_id <= 0:
            return False
        user = self.getUser(user_id)
        return user.nChannelID == bot_channel_id

    def _require_user_in_bot_channel(self, user_id: int) -> bool:
        if self._user_in_bot_channel(user_id):
            return True
        self._reply(user_id, "You need to be in the same channel as the bot to synthesize text.")
        return False

    def _reply(self, user_id: int, content: str) -> None:
        if user_id <= 0:
            # A request from the web page: there is no one to message.
            self._log(content)
            return
        if threading.current_thread() is not self:
            self.actions.put(lambda: self._reply(user_id, content))
            return
        for message in tt.buildTextMessage(
            content,
            tt.TextMsgType.MSGTYPE_USER,
            nToUserID=user_id,
            nFromUserID=self._my_user_id,
        ):
            self.doTextMessage(message)

    def _help_text(self) -> str:
        p = self.bot_settings.command_prefix
        return (
            "STAR bot commands:\n"
            f"{p}voices [filter] - list available voices\n"
            f"{p}voice <name or number> - choose your voice\n"
            "/rate <number|none> - set or clear your speech rate\n"
            "/pitch <number|none> - set or clear your speech pitch\n"
            f"{p}name - toggle announcing your name before your messages\n"
            f"{p}preview [voice] - speak the configured preview text\n"
            f"{p}current - show your chosen voice\n"
            f"{p}status - show bot status\n"
            f"{p}stop - stop current stream and clear this server's queue\n"
            f"{p}restart - reconnect the bot to this server, with its settings read again\n"
            f"{p}quit - disconnect the bot from this server until the bot is restarted\n"
            f"Any private message without {p} is spoken with your chosen voice."
        )

    def onConnectSuccess(self):
        self.state = "logging_in"
        self.doLogin(
            tt.ttstr(self.server.nickname),
            tt.ttstr(self.server.username),
            tt.ttstr(self.server.password),
            tt.ttstr(CLIENT_NAME),
        )

    def onConnectFailed(self):
        self._log("Connection failed", "WARNING")
        self.reconnect_now = True

    def onConnectCryptError(self, clienterrormsg):
        self._log(f"Encryption error: {tt.ttstr(clienterrormsg.szErrorMsg)}", "WARNING")
        self.reconnect_now = True

    def onConnectionLost(self):
        self._log("Connection lost", "WARNING")
        self.reconnect_now = True

    def onCmdError(self, cmdId: int, errmsg):
        self._log(f"Command {cmdId} failed with error {errmsg.nErrorNo}: {tt.ttstr(errmsg.szErrorMsg)}", "WARNING")

    def onCmdMyselfLoggedIn(self, userid: int, useraccount):
        self.logged_in = True
        self._my_user_id = userid
        self.state = "joining"
        self._log(f"Logged in as user {userid}")
        self._join_configured_channel()

    def onCmdMyselfLoggedOut(self):
        self.state = "reconnecting"
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0

    def onCmdMyselfKickedFromChannel(self, channelid: int, user):
        self.in_channel = False
        self.current_channel_id = 0
        self.state = "joining"
        self._log(f"Kicked from channel {channelid}", "WARNING")

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = True
            self.current_channel_id = user.nChannelID
            self.pending_channel_join = False
            self.state = "connected"
            self.audio_format = self._channel_audio_format(user.nChannelID)
            self._log(f"Joined channel {tt.ttstr(self.getChannelPath(user.nChannelID))}")
            self._start_next_media()

    def onCmdUserLeftChannel(self, channelid: int, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = False
            self.current_channel_id = 0
            self.state = "joining"

    def onCmdUserUpdate(self, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = user.nChannelID > 0
            self.current_channel_id = user.nChannelID
            self.state = "connected" if self.in_channel else "joining"

    def onCmdUserTextMessage(self, textmessage):
        if textmessage.nMsgType != tt.TextMsgType.MSGTYPE_USER:
            return
        if textmessage.nFromUserID == self._my_user_id:
            return
        key = (textmessage.nFromUserID, textmessage.nMsgType, textmessage.nChannelID)
        if textmessage.bMore:
            self.text_parts.setdefault(key, []).append(textmessage)
            return
        if key in self.text_parts:
            parts = self.text_parts.pop(key)
            parts.append(textmessage)
            content = tt.rebuildTextMessage(parts)
        else:
            content = str(tt.ttstr(textmessage.szMessage))
        self._handle_private_message(textmessage.nFromUserID, content.strip())

    def getMessage(self, nWaitMS: int = -1):
        # The SDK's runEventLoop does not dispatch CLIENTEVENT_AUDIOINPUT,
        # so it is picked up here, as each message is read.
        msg = tt.TeamTalk.getMessage(self, nWaitMS)
        if msg.nClientEvent == tt.ClientEvent.CLIENTEVENT_AUDIOINPUT:
            progress = msg.audioinputprogress
            self._on_audio_input(progress.nStreamID, progress.uQueueMSec, progress.uElapsedMSec)
        return msg

    def _on_audio_input(self, stream_id: int, queue_msec: int, elapsed_msec: int) -> None:
        if self.current_media is None or stream_id != self._stream_id:
            return
        if queue_msec == 0 and elapsed_msec == 0:
            # The session ended: everything was sent.
            self.current_media = None
            self._stream_id = 0
            self._start_next_media()
            return
        self._elapsed_msec = max(self._elapsed_msec, elapsed_msec)
        self._feed_audio()

    def _feed_audio(self) -> None:
        """Tops up TeamTalk's audio input queue for current_media; when all
        of it is queued, flushes it with an empty block (which ends the
        session once the queue has been sent)."""
        speech = self.current_media
        if speech is None or self._flushed:
            return
        chunk_bytes = speech.frame_bytes * (speech.sample_rate * AUDIO_CHUNK_MSEC // 1000)
        bytes_per_msec = speech.frame_bytes * speech.sample_rate / 1000
        while (self._sent_bytes < len(speech.pcm)
               and self._sent_bytes / bytes_per_msec - self._elapsed_msec < AUDIO_AHEAD_MSEC):
            piece = speech.pcm[self._sent_bytes:self._sent_bytes + chunk_bytes]
            buffer = ctypes.create_string_buffer(piece, len(piece))
            block = tt.AudioBlock()
            block.nStreamID = self._stream_id
            block.nSampleRate = speech.sample_rate
            block.nChannels = speech.channels
            block.lpRawAudio = ctypes.cast(buffer, ctypes.c_void_p)
            block.nSamples = len(piece) // speech.frame_bytes
            block.uSampleIndex = 0
            block.uStreamTypes = tt.StreamType.STREAMTYPE_VOICE
            if not self.insertAudioBlock(block):
                if self._sent_bytes == 0:
                    self._speech_failed("TeamTalk did not accept the audio. Is the bot allowed to transmit voice in this channel?")
                return  # the queue is full: more when TeamTalk reports progress
            self._sent_bytes += len(piece)
        if self._sent_bytes >= len(speech.pcm):
            empty = tt.AudioBlock()
            empty.nStreamID = self._stream_id
            empty.nSampleRate = speech.sample_rate
            empty.nChannels = speech.channels
            empty.lpRawAudio = None
            empty.nSamples = 0
            empty.uStreamTypes = tt.StreamType.STREAMTYPE_VOICE
            self.insertAudioBlock(empty)
            self._flushed = True

    def _speech_failed(self, reason: str) -> None:
        speech = self.current_media
        self._shutdown_current()
        if speech:
            self._reply(speech.requester_id, f"Could not speak {speech.description}: {reason}")
            self._log(f"Could not speak {speech.description}: {reason}", "WARNING")
        self._start_next_media()

    def _shutdown_current(self) -> None:
        if self.current_media is not None:
            try:
                self.insertAudioBlock(None)
            except Exception:
                pass
        self.current_media = None
        self._stream_id = 0

    def _check_speech_deadline(self) -> None:
        # A safety net: if TeamTalk never reports the end, move on.
        if self.current_media is not None and time.monotonic() > self._speech_deadline:
            self._log(f"Speech did not finish in time; going on: {self.current_media.description}", "WARNING")
            self._shutdown_current()
            self._start_next_media()

    def _channel_audio_format(self, channel_id: int) -> tuple[int, int]:
        codec = self.getChannel(channel_id).audiocodec
        if codec.nCodec == tt.Codec.OPUS_CODEC and codec.opus.nSampleRate > 0:
            return codec.opus.nSampleRate, max(1, min(2, codec.opus.nChannels))
        if codec.nCodec == tt.Codec.SPEEX_CODEC:
            return (8000, 16000, 32000)[max(0, min(2, codec.speex.nBandmode))], 1
        if codec.nCodec == tt.Codec.SPEEX_VBR_CODEC:
            return (8000, 16000, 32000)[max(0, min(2, codec.speex_vbr.nBandmode))], 1
        return 48000, 1

    def _join_configured_channel(self) -> None:
        channel = self.server.channel.strip()
        if not channel:
            return
        channel_id = self.getRootChannelID() if channel == "/" else self.getChannelIDFromPath(tt.ttstr(channel))
        if channel_id <= 0:
            self._log(f"Channel not found: {channel}", "WARNING")
            self.state = "no_channel"
            return
        self.pending_channel_join = True
        self.doJoinChannelByID(channel_id, tt.ttstr(self.server.channel_password))

    def _handle_private_message(self, user_id: int, content: str) -> None:
        if not content:
            return
        prefix = self.bot_settings.command_prefix
        command_text = ""
        if content.startswith(prefix):
            command_text = content[len(prefix) :].strip()
        elif content.startswith("/"):
            command_text = content[1:].strip()

        if command_text:
            command, _, rest = command_text.partition(" ")
            self._handle_command(user_id, command.lower(), rest.strip())
            return

        self._speak_for_user(user_id, content)

    def _handle_command(self, user_id: int, command: str, rest: str) -> None:
        if command in {"help", "h", "?"}:
            self._reply(user_id, self._help_text())
        elif command in {"voices", "v"}:
            self._list_voices(user_id, rest)
        elif command in {"voice", "select"}:
            self._select_voice(user_id, rest)
        elif command in {"current", "who"}:
            self._reply(user_id, self._current_settings_text(user_id))
        elif command == "rate":
            self._set_user_numeric_setting(user_id, rest, self.user_rates, "rate")
        elif command == "pitch":
            self._set_user_numeric_setting(user_id, rest, self.user_pitches, "pitch")
        elif command == "name":
            self._toggle_name_announcements(user_id)
        elif command == "preview":
            voice = rest or self._current_voice(user_id)
            if not voice:
                self._reply(user_id, "Choose a voice first.")
                return
            text = self.bot_settings.voice_preview_text.format(voice=voice)
            self._request_synthesis(user_id, voice, text)
        elif command == "status":
            self._reply(
                user_id,
                f"STAR connections: {self.star_pool.connected_count()}/{len(self.star_pool.connections)}. "
                f"Voices: {len(self.star_pool.all_voices())}. "
                f"Queued streams on this server: {self.media_queue.qsize() + (1 if self.current_media else 0)}.",
            )
        elif command == "stop":
            self._shutdown_streaming()
            self._reply(user_id, "Stopped streaming and cleared this server's queue.")
        elif command in {"quit", "exit", "shutdown"}:
            # Only this server: the other servers keep running. Restarting
            # the whole bot is done with startman or on the web page.
            self._reply(user_id, "Disconnecting from this server until the bot is restarted.")
            self._log(f"/quit from {self._user_display_name(user_id)}: disconnecting from this server")
            if self.manager:
                self.manager.stop_server(self.server.name, reason="disconnected by /quit")
            else:
                self.stop()
        elif command == "restart":
            self._reply(user_id, "Reconnecting to this server.")
            self._log(f"/restart from {self._user_display_name(user_id)}: reconnecting to this server")
            if self.manager:
                self.manager.restart_server(self.server.name)
            else:
                self.reconnect_now = True
        else:
            self._reply(user_id, f"Unknown command. Send {self.bot_settings.command_prefix}help for commands.")

    def _current_voice(self, user_id: int) -> str:
        user_key = self._user_key(user_id)
        return self.user_voices.get(user_key) or self.bot_settings.default_voice

    def _current_settings_text(self, user_id: int) -> str:
        user_key = self._user_key(user_id)
        voice = self._current_voice(user_id)
        rate = self.user_rates.get(user_key, "none")
        pitch = self.user_pitches.get(user_key, "none")
        voice_text = voice if voice else "none"
        names = "on" if user_key in self.users_announcing_names else "off"
        return f"Current voice: {voice_text}. Rate: {rate}. Pitch: {pitch}. Name: {names}."

    def _toggle_name_announcements(self, user_id: int) -> None:
        user_key = self._user_key(user_id)
        if user_key in self.users_announcing_names:
            self.users_announcing_names.discard(user_key)
            self._reply(user_id, "Name announcements off. Your messages will be spoken without your name.")
        else:
            self.users_announcing_names.add(user_key)
            self._reply(user_id, "Name announcements on. Your messages will start with your name.")

    def _set_user_numeric_setting(
        self,
        user_id: int,
        value: str,
        settings: dict[str, str],
        setting_name: str,
    ) -> None:
        user_key = self._user_key(user_id)
        value = value.strip()
        if not value:
            current = settings.get(user_key, "none")
            self._reply(user_id, f"Current {setting_name}: {current}. Usage: /{setting_name} number or /{setting_name} none.")
            return
        if value.lower() == "none":
            settings.pop(user_key, None)
            self._reply(user_id, f"{setting_name.title()} cleared.")
            return
        try:
            parsed = float(value)
        except ValueError:
            self._reply(user_id, f"{setting_name.title()} must be a number, or none to clear it.")
            return
        if not math.isfinite(parsed):
            self._reply(user_id, f"{setting_name.title()} must be a finite number, or none to clear it.")
            return
        settings[user_key] = value
        self._reply(user_id, f"{setting_name.title()} set to {value}.")

    def _voice_with_user_params(self, user_id: int, voice: str) -> str:
        user_key = self._user_key(user_id)
        params = []
        if user_key in self.user_rates:
            params.append(f"r={self.user_rates[user_key]}")
        if user_key in self.user_pitches:
            params.append(f"p={self.user_pitches[user_key]}")
        if not params:
            return voice
        param_text = " ".join(params)
        return f"{voice}<{param_text}>"

    def _list_voices(self, user_id: int, filter_text: str) -> None:
        voices = self.star_pool.all_voices()
        if filter_text:
            query = filter_text.lower()
            voices = [voice for voice in voices if query in voice.lower()]
        self.user_last_results[self._user_key(user_id)] = voices
        if not voices:
            self._reply(user_id, "No matching voices are available.")
            return
        limit = max(1, self.bot_settings.max_voice_results)
        lines = [f"{index + 1}. {voice}" for index, voice in enumerate(voices[:limit])]
        suffix = ""
        if len(voices) > limit:
            suffix = f"\nShowing {limit} of {len(voices)}. Use a filter to narrow the list."
        self._reply(user_id, "\n".join(lines) + suffix)

    def _select_voice(self, user_id: int, selector: str) -> None:
        if not selector:
            current = self._current_voice(user_id)
            self._reply(
                user_id,
                f"Current voice: {current}"
                if current
                else f"Usage: {self.bot_settings.command_prefix}voice name",
            )
            return
        user_key = self._user_key(user_id)
        voice = self._resolve_voice(user_id, user_key, selector)
        if voice is None:
            return
        self.user_voices[user_key] = voice
        self._reply(user_id, f"Voice set to {voice}.")

    def _resolve_voice(self, user_id: int, user_key: str, selector: str) -> str | None:
        selector = selector.strip()
        if selector.isdigit():
            results = self.user_last_results.get(user_key, [])
            index = int(selector) - 1
            if 0 <= index < len(results):
                return results[index]
            self._reply(user_id, "That voice number is not in your last voice list.")
            return None
        voices = self.star_pool.all_voices()
        exact = [voice for voice in voices if voice.lower() == selector.lower()]
        if exact:
            return exact[0]
        matches = [voice for voice in voices if selector.lower() in voice.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            self._reply(user_id, f"No voice matched {selector}.")
            return None
        self.user_last_results[user_key] = matches
        limit = max(1, self.bot_settings.max_voice_results)
        lines = [f"{index + 1}. {voice}" for index, voice in enumerate(matches[:limit])]
        suffix = ""
        if len(matches) > limit:
            suffix = f"\nShowing {limit} of {len(matches)}. Use a more specific voice name."
        self._reply(
            user_id,
            f"Multiple voices matched. Reply with {self.bot_settings.command_prefix}voice number:\n"
            + "\n".join(lines)
            + suffix,
        )
        return None

    def _speak_for_user(self, user_id: int, text: str) -> None:
        voice = self._current_voice(user_id)
        if not voice:
            self._reply(
                user_id,
                f"Choose a voice first with {self.bot_settings.command_prefix}voice <name>. "
                f"Use {self.bot_settings.command_prefix}voices to list voices.",
            )
            return
        if self._user_key(user_id) in self.users_announcing_names:
            text = f"{self._user_display_name(user_id)} said, {text}"
        self._request_synthesis(user_id, voice, text)

    def _request_synthesis(self, user_id: int, voice: str, text: str) -> None:
        if not self._require_user_in_bot_channel(user_id):
            return
        voice_for_request = self._voice_with_user_params(user_id, voice)
        self._reply(user_id, f"Synthesizing with {voice}.")

        def done(error: str | None, extension: str | None, audio: bytes | None) -> None:
            if error:
                self._reply(user_id, f"Synthesis failed: {error}")
                return
            if not audio:
                self._reply(user_id, "Synthesis failed: empty audio response.")
                return
            self._prepare_speech(user_id, voice, extension or "wav", audio)

        if not self.star_pool.request(voice_for_request, text, done):
            self._reply(user_id, "No connected STAR coagulator is available.")

    def _prepare_speech(self, requester_id: int, description: str, extension: str, audio: bytes) -> None:
        """Decodes synthesized audio on its own thread (never the TeamTalk or
        STAR thread), then queues it on the connection's thread."""
        self._decode_queue.put((requester_id, description, extension, audio))
        if self._decoder is None or not self._decoder.is_alive():
            self._decoder = threading.Thread(target=self._decode_worker, name=f"decode:{self.server.name}", daemon=True)
            self._decoder.start()

    def _decode_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                requester_id, description, extension, audio = self._decode_queue.get(timeout=1)
            except queue.Empty:
                continue
            sample_rate, channels = self.audio_format
            try:
                pcm = decode_audio(audio, extension, sample_rate, channels, self.bot_settings.temp_dir)
            except Exception as exc:
                self._reply(requester_id, f"Could not decode the synthesized audio: {exc}")
                self._log(f"Could not decode the synthesized audio for {description}: {exc}", "WARNING")
                continue
            pcm += b"\0" * (2 * channels * (sample_rate * SPEECH_TAIL_MSEC // 1000))
            speech = Speech(pcm, sample_rate, channels, requester_id, description)
            self.actions.put(lambda speech=speech: self._enqueue_media(speech))

    def _enqueue_media(self, speech: Speech) -> None:
        if not self.in_channel:
            self._reply(
                speech.requester_id,
                "Synthesis finished, but the bot is not in a TeamTalk channel to speak it.",
            )
            return
        self.media_queue.put(speech)
        self._start_next_media()

    def _start_next_media(self) -> None:
        if self.current_media or not self.in_channel:
            return
        try:
            speech = self.media_queue.get_nowait()
        except queue.Empty:
            return
        if (speech.sample_rate, speech.channels) != self.audio_format:
            # The channel changed since it was decoded; TeamTalk resamples.
            self._log(f"Speech decoded for {speech.sample_rate} Hz/{speech.channels} ch, channel uses "
                      f"{self.audio_format[0]} Hz/{self.audio_format[1]} ch")
        self.current_media = speech
        self._stream_id = next(self._stream_ids)
        self._sent_bytes = 0
        self._flushed = False
        self._elapsed_msec = 0
        self._speech_deadline = time.monotonic() + speech.seconds + 15
        self._feed_audio()


class ServerError(Exception):
    """A change to the servers that cannot be made (shown to the user)."""


class BotManager:
    """Runs one connection per enabled server and changes them while the bot
    runs: the web interface, /restart and /quit go through here, so a
    change to one server never touches the others."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = read_config(config_path)
        self._lock = threading.RLock()
        self.connections: dict[str, TeamTalkBotConnection] = {}
        # Why an enabled server is not running (e.g. "disconnected by /quit").
        self.not_running: dict[str, str] = {}
        self.stop_event = threading.Event()
        self.restart_requested = False
        self.process_started_at = time.time()
        if self.config.teamtalk.license_name or self.config.teamtalk.license_key:
            tt.setLicense(tt.ttstr(self.config.teamtalk.license_name), tt.ttstr(self.config.teamtalk.license_key))
        self.star_pool = StarPool(self.config.star)
        self.star_pool.start()

    # ------------------------------------------------------------ servers

    def server(self, name: str) -> TeamTalkServerSettings:
        for server in self.config.servers:
            if server.name == name:
                return server
        raise ServerError(f"There is no server named {name}.")

    def start_all(self) -> None:
        for server in self.config.servers:
            if server.enabled:
                self.start_server(server.name)

    def start_server(self, name: str) -> bool:
        with self._lock:
            server = self.server(name)
            if name in self.connections:
                return True
            if not server.enabled:
                return False
            connection = TeamTalkBotConnection(server, self.config.teamtalk, self.config.bot, self.star_pool, self)
            self.connections[name] = connection
            self.not_running.pop(name, None)
            connection.start()
            return True

    def _stop(self, name: str, reason: str = "") -> None:
        with self._lock:
            connection = self.connections.pop(name, None)
            if reason:
                self.not_running[name] = reason
        if connection is not None:
            connection.stop()
            if connection is not threading.current_thread():
                connection.join(timeout=10)
            log("Disconnected", server=name)

    def stop_server(self, name: str, reason: str = "", delay: float = 0.5) -> None:
        """Disconnects one server in the background (a command can stop its
        own connection); `delay` lets a reply reach the user first."""

        def run() -> None:
            time.sleep(delay)
            self._stop(name, reason)

        threading.Thread(target=run, name=f"stop:{name}", daemon=True).start()

    def restart_server(self, name: str, delay: float = 0.5) -> None:
        """Reconnects one server in the background, its settings read again
        from the configuration file."""

        def run() -> None:
            time.sleep(delay)
            try:
                self.reload_and_restart(name)
            except ServerError as exc:
                log(f"Not reconnected: {exc}", server=name, level="WARNING")

        threading.Thread(target=run, name=f"restart:{name}", daemon=True).start()

    def reload_and_restart(self, name: str) -> bool:
        self._stop(name)
        with self._lock:
            try:
                fresh = read_config(self.config_path)
            except (configparser.Error, ValueError) as exc:
                self.not_running[name] = f"the configuration file could not be read: {exc}"
                raise ServerError(f"The configuration file could not be read: {exc}")
            replacement = next((s for s in fresh.servers if s.name == name), None)
            if replacement is None:
                raise ServerError(f"{name} is no longer in the configuration file.")
            self.config.servers = [replacement if s.name == name else s for s in self.config.servers]
            return self.start_server(name)

    def _validate(self, settings: TeamTalkServerSettings) -> None:
        if not SERVER_NAME.match(settings.name):
            raise ServerError(
                "The server name may use letters, numbers, dots, dashes and underscores, "
                "starting with a letter or number, at most 40 characters."
            )
        if not settings.host.strip():
            raise ServerError("Fill in the server address.")
        for label, port in (("TCP port", settings.tcp_port), ("UDP port", settings.udp_port)):
            if not 1 <= port <= 65535:
                raise ServerError(f"The {label} must be between 1 and 65535.")
        if not settings.nickname.strip():
            raise ServerError("Fill in the bot's nickname.")

    def add_server(self, settings: TeamTalkServerSettings) -> bool:
        self._validate(settings)
        with self._lock:
            if any(s.name == settings.name for s in self.config.servers):
                raise ServerError(f"There is already a server named {settings.name}.")
            self.config.servers.append(settings)
            self.save()
            log(f"Server added ({settings.host}:{settings.tcp_port})", server=settings.name)
            return self.start_server(settings.name) if settings.enabled else False

    def update_server(self, name: str, settings: TeamTalkServerSettings) -> bool:
        settings.name = name
        self._validate(settings)
        self._stop(name)
        with self._lock:
            self.server(name)
            self.config.servers = [settings if s.name == name else s for s in self.config.servers]
            self.save()
            log("Settings changed", server=name)
            return self.start_server(name) if settings.enabled else False

    def remove_server(self, name: str) -> None:
        self.server(name)
        self._stop(name)
        with self._lock:
            self.config.servers = [s for s in self.config.servers if s.name != name]
            self.not_running.pop(name, None)
            self.save()
        log("Server removed", server=name)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            server = self.server(name)
            server.enabled = enabled
            self.save()
        if enabled:
            log("Turned on", server=name)
            return self.start_server(name)
        self._stop(name)
        self.not_running.pop(name, None)
        log("Turned off", server=name)
        return False

    # ----------------------------------------------------------- settings

    def update_settings(self, star: StarSettings, bot: BotSettings, teamtalk: TeamTalkGlobalSettings) -> list[str]:
        """Applies the bot-wide settings; returns what still needs a restart
        of the whole bot."""
        needs_restart = []
        with self._lock:
            old = self.config
            # Shared objects: every connection sees these at once.
            for key, value in vars(bot).items():
                setattr(old.bot, key, value)
            old.teamtalk.reconnect_seconds = teamtalk.reconnect_seconds
            if (teamtalk.license_name, teamtalk.license_key) != (old.teamtalk.license_name, old.teamtalk.license_key):
                needs_restart.append("the SDK license")
                old.teamtalk.license_name = teamtalk.license_name
                old.teamtalk.license_key = teamtalk.license_key
            if (star.hosts, star.request_timeout) != (old.star.hosts, old.star.request_timeout):
                self._replace_star_pool(star)
            self.save()
        log("Bot settings changed")
        return needs_restart

    def _replace_star_pool(self, star: StarSettings) -> None:
        old_pool = self.star_pool
        self.config.star = star
        self.star_pool = StarPool(star)
        self.star_pool.start()
        for connection in list(self.connections.values()):
            connection.star_pool = self.star_pool
        old_pool.stop()
        log("STAR coagulators changed: " + ", ".join(star.hosts))

    def save(self) -> None:
        write_config(self.config_path, self.config)

    # ------------------------------------------------------------ process

    def restart_process(self) -> None:
        """Restarts the whole bot (every server reconnects)."""
        self.restart_requested = True
        self.stop_event.set()

    def close(self) -> None:
        with self._lock:
            names = list(self.connections)
        for connection in [self.connections.get(name) for name in names]:
            if connection is not None:
                connection.stop()
        for name in names:
            connection = self.connections.pop(name, None)
            if connection is not None:
                connection.join(timeout=5)
        self.star_pool.stop()
        self.star_pool.join(timeout=2)


def run_bot(config_path: Path) -> int:
    manager = BotManager(config_path)
    config = manager.config
    if not config.servers and not config.web.enabled:
        print(f"No TeamTalk servers are configured in {config_path}. Run with --configure first.")
        manager.close()
        return 2
    if not any(server.enabled for server in config.servers) and not config.web.enabled:
        print("All TeamTalk server sections are disabled.")
        manager.close()
        return 2

    web = None
    if config.web.enabled:
        import web as web_interface

        try:
            web = web_interface.WebInterface(manager, config.web.host, config.web.port)
            web.start()
        except OSError as exc:
            log(f"The web interface could not start on {config.web.host}:{config.web.port}: {exc}", level="ERROR")
            web = None

    manager.start_all()
    log("STAR TeamTalk bot is running. Press Ctrl+C to stop.")
    try:
        while not manager.stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        log("Stopping...")
    finally:
        if web is not None:
            web.close()
        manager.close()
    if manager.restart_requested:
        log("Restarting the bot process")
        return EXIT_RESTART
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STAR TeamTalk bot user client")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.ini")
    parser.add_argument("--configure", action="store_true", help="Interactively create or update config.ini")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser().resolve()
    if args.configure:
        configure(config_path)
        return 0
    exit_code = run_bot(config_path)
    if exit_code == EXIT_RESTART:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
