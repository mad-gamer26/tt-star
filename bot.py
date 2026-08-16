#!/usr/bin/env python3
"""STAR TeamTalk bot.

This is a headless STAR user client that listens for private TeamTalk
messages, synthesizes text through a STAR coagulator, and streams each
generated audio file into the bot's current TeamTalk channel.
"""

from __future__ import annotations

import argparse
import configparser
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import queue
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

tt = load_teamtalk_sdk()


USER_REVISION = 4
DEFAULT_CONFIG = SCRIPT_DIR / "config.ini"
EXIT_RESTART = 75
DONE_MEDIA_STATUSES = {
    tt.MediaFileStatus.MFS_FINISHED,
    tt.MediaFileStatus.MFS_ERROR,
    tt.MediaFileStatus.MFS_ABORTED,
}


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
    client_name: str = "STAR TeamTalk Bot"
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
class AppConfig:
    star: StarSettings
    bot: BotSettings
    teamtalk: TeamTalkGlobalSettings
    servers: list[TeamTalkServerSettings]


class RuntimeControl:
    def __init__(self):
        self.stop_event = threading.Event()
        self._restart_requested = False
        self._lock = threading.Lock()

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_restart(self) -> None:
        with self._lock:
            self._restart_requested = True
        self.stop_event.set()

    def restart_requested(self) -> bool:
        with self._lock:
            return self._restart_requested


def read_config(path: Path) -> AppConfig:
    parser = configparser.ConfigParser()
    parser.read(path)

    star_section = parser["star"] if parser.has_section("star") else {}
    bot_section = parser["bot"] if parser.has_section("bot") else {}
    tt_section = parser["teamtalk"] if parser.has_section("teamtalk") else {}

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
        client_name=tt_section.get("client_name", "STAR TeamTalk Bot"),
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

    return AppConfig(star=star, bot=bot, teamtalk=teamtalk, servers=servers)


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
        "client_name": config.teamtalk.client_name,
        "reconnect_seconds": str(config.teamtalk.reconnect_seconds),
        "license_name": config.teamtalk.license_name,
        "license_key": config.teamtalk.license_key,
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

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        parser.write(fp)


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
        temp_dir=prompt("Temp directory for synthesized audio files", existing.bot.temp_dir),
    )
    teamtalk = TeamTalkGlobalSettings(
        client_name=prompt("TeamTalk client name", existing.teamtalk.client_name),
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

    write_config(path, AppConfig(star=star, bot=bot, teamtalk=teamtalk, servers=servers))
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
                    print(f"Connected STAR coagulator {self.display_name}: {self.host}")
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
                    print(f"STAR {self.display_name} reconnecting to {self.host!r}: {exc}")
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
            print(f"Invalid STAR message from {self.display_name}: {message}")
            return
        if "voices" in payload:
            with self._lock:
                self.voices = normalize_voices(payload["voices"])
            print(f"STAR {self.display_name} has {len(self.voices)} voices")
        elif "status" in payload or "error" in payload or "warning" in payload:
            request_id = normalize_request_id(payload.get("id", ""))
            text = payload.get("status") or payload.get("error") or payload.get("warning")
            if request_id and request_id in self.pending:
                _, callback, _ = self.pending.pop(request_id)
                callback(text, None, None)
            else:
                print(f"STAR {self.display_name}: {text}")

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
        control: RuntimeControl,
    ):
        tt.TeamTalk.__init__(self)
        threading.Thread.__init__(self, name=f"teamtalk:{server.name}", daemon=True)
        self.server = server
        self.global_settings = global_settings
        self.bot_settings = bot_settings
        self.star_pool = star_pool
        self.control = control
        self.stop_event = threading.Event()
        self.actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self.user_voices: dict[str, str] = {}
        self.user_rates: dict[str, str] = {}
        self.user_pitches: dict[str, str] = {}
        self.user_last_results: dict[str, list[str]] = {}
        self.text_parts: dict[tuple[int, int, int], list[tt.TextMessage]] = {}
        self.media_queue: queue.Queue[tuple[Path, int, str]] = queue.Queue()
        self.current_media: tuple[Path, int, str] | None = None
        self.pending_channel_join = False
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0
        self.reconnect_now = False
        self._my_user_id = 0

    def stop(self) -> None:
        self.stop_event.set()
        self.actions.put(lambda: self._shutdown_streaming())

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._reset_runtime()
                print(f"Connecting TeamTalk {self.server.name}: {self.server.host}:{self.server.tcp_port}")
                ok = self.connect(
                    tt.ttstr(self.server.host),
                    self.server.tcp_port,
                    self.server.udp_port,
                    bEncrypted=self.server.encrypted,
                )
                if not ok:
                    print(f"TeamTalk {self.server.name}: connect call failed")
                    self.stop_event.wait(self.global_settings.reconnect_seconds)
                    continue
                while not self.stop_event.is_set() and not self.reconnect_now:
                    self._drain_actions()
                    self.runEventLoop(100)
                self._shutdown_streaming()
                self.disconnect()
            except Exception:
                traceback.print_exc()
            if not self.stop_event.is_set():
                self.stop_event.wait(self.global_settings.reconnect_seconds)

    def _reset_runtime(self) -> None:
        self.reconnect_now = False
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0
        self.pending_channel_join = False
        self.text_parts.clear()
        self.current_media = None
        while not self.media_queue.empty():
            try:
                media_path, _, _ = self.media_queue.get_nowait()
            except queue.Empty:
                break
            self._delete_temp_file(media_path)

    def _drain_actions(self) -> None:
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                return
            action()

    def _shutdown_streaming(self) -> None:
        try:
            self.stopStreamingMediaFileToChannel()
        except Exception:
            pass
        if self.current_media:
            self._delete_temp_file(self.current_media[0])
            self.current_media = None
        while not self.media_queue.empty():
            try:
                media_path, _, _ = self.media_queue.get_nowait()
            except queue.Empty:
                break
            self._delete_temp_file(media_path)

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
            f"{p}preview [voice] - speak the configured preview text\n"
            f"{p}current - show your chosen voice\n"
            f"{p}status - show bot status\n"
            f"{p}stop - stop current stream and clear this server's queue\n"
            f"{p}restart - restart the bot process\n"
            f"{p}quit - shut down the bot process\n"
            f"Any private message without {p} is spoken with your chosen voice."
        )

    def onConnectSuccess(self):
        self.doLogin(
            tt.ttstr(self.server.nickname),
            tt.ttstr(self.server.username),
            tt.ttstr(self.server.password),
            tt.ttstr(self.global_settings.client_name),
        )

    def onConnectFailed(self):
        print(f"TeamTalk {self.server.name}: connection failed")
        self.reconnect_now = True

    def onConnectCryptError(self, clienterrormsg):
        print(f"TeamTalk {self.server.name}: encryption error {clienterrormsg.szErrorMsg}")
        self.reconnect_now = True

    def onConnectionLost(self):
        print(f"TeamTalk {self.server.name}: connection lost")
        self.reconnect_now = True

    def onCmdError(self, cmdId: int, errmsg):
        print(f"TeamTalk {self.server.name}: command {cmdId} error {errmsg.nErrorNo}: {tt.ttstr(errmsg.szErrorMsg)}")

    def onCmdMyselfLoggedIn(self, userid: int, useraccount):
        self.logged_in = True
        self._my_user_id = userid
        print(f"TeamTalk {self.server.name}: logged in as user {userid}")
        self._join_configured_channel()

    def onCmdMyselfLoggedOut(self):
        self.logged_in = False
        self.in_channel = False
        self.current_channel_id = 0

    def onCmdMyselfKickedFromChannel(self, channelid: int, user):
        self.in_channel = False
        self.current_channel_id = 0
        print(f"TeamTalk {self.server.name}: kicked from channel {channelid}")

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = True
            self.current_channel_id = user.nChannelID
            self.pending_channel_join = False
            print(f"TeamTalk {self.server.name}: joined channel {user.nChannelID}")
            self._start_next_media()

    def onCmdUserLeftChannel(self, channelid: int, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = False
            self.current_channel_id = 0

    def onCmdUserUpdate(self, user):
        if user.nUserID == self._my_user_id:
            self.in_channel = user.nChannelID > 0
            self.current_channel_id = user.nChannelID

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

    def onStreamMediaFile(self, mediafileinfo):
        status = mediafileinfo.nStatus
        if status == tt.MediaFileStatus.MFS_STARTED:
            return
        if status not in DONE_MEDIA_STATUSES:
            return
        finished = self.current_media
        self.current_media = None
        if finished:
            media_path, requester_id, description = finished
            self._delete_temp_file(media_path)
            if status == tt.MediaFileStatus.MFS_ERROR:
                self._reply(requester_id, f"Streaming failed for {description}.")
            elif status == tt.MediaFileStatus.MFS_ABORTED:
                self._reply(requester_id, f"Streaming aborted for {description}.")
        self._start_next_media()

    def _join_configured_channel(self) -> None:
        channel = self.server.channel.strip()
        if not channel:
            return
        channel_id = self.getRootChannelID() if channel == "/" else self.getChannelIDFromPath(tt.ttstr(channel))
        if channel_id <= 0:
            print(f"TeamTalk {self.server.name}: channel not found: {channel}")
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
            self._reply(user_id, "Shutting down.")
            self.control.request_stop()
        elif command == "restart":
            self._reply(user_id, "Restarting.")
            self.control.request_restart()
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
        return f"Current voice: {voice_text}. Rate: {rate}. Pitch: {pitch}."

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
        speaker = self._user_display_name(user_id)
        self._request_synthesis(user_id, voice, f"{speaker} said, {text}")

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
            try:
                temp_path = self._write_temp_audio(extension or "wav", audio)
            except Exception as exc:
                self._reply(user_id, f"Could not write temp audio file: {exc}")
                return
            self.actions.put(lambda: self._enqueue_media(temp_path, user_id, voice))

        if not self.star_pool.request(voice_for_request, text, done):
            self._reply(user_id, "No connected STAR coagulator is available.")

    def _write_temp_audio(self, extension: str, audio: bytes) -> Path:
        extension = "".join(char for char in extension.lower() if char.isalnum()) or "wav"
        temp_dir = self.bot_settings.temp_dir or None
        if temp_dir:
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="star_tt_",
            suffix=f".{extension}",
            dir=temp_dir,
            delete=False,
        ) as fp:
            fp.write(audio)
            return Path(fp.name)

    def _enqueue_media(self, path: Path, requester_id: int, description: str) -> None:
        if not self.in_channel:
            self._delete_temp_file(path)
            self._reply(
                requester_id,
                "Synthesis finished, but the bot is not in a TeamTalk channel to stream it.",
            )
            return
        self.media_queue.put((path, requester_id, description))
        self._start_next_media()

    def _start_next_media(self) -> None:
        if self.current_media or not self.in_channel:
            return
        try:
            self.current_media = self.media_queue.get_nowait()
        except queue.Empty:
            return
        path, requester_id, description = self.current_media
        if not path.exists():
            self.current_media = None
            self._reply(requester_id, f"Temp audio file missing for {description}.")
            self._start_next_media()
            return
        if not self.startStreamingMediaFileToChannel(tt.ttstr(str(path)), None):
            self.current_media = None
            self._delete_temp_file(path)
            self._reply(requester_id, f"Could not start TeamTalk media stream for {description}.")
            self._start_next_media()

    def _delete_temp_file(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"Could not delete temp file {path}: {exc}")


def run_bot(config_path: Path) -> int:
    config = read_config(config_path)
    if not config.servers:
        print(f"No TeamTalk servers are configured in {config_path}. Run with --configure first.")
        return 2

    if config.teamtalk.license_name or config.teamtalk.license_key:
        tt.setLicense(tt.ttstr(config.teamtalk.license_name), tt.ttstr(config.teamtalk.license_key))

    star_pool = StarPool(config.star)
    star_pool.start()
    control = RuntimeControl()

    connections = [
        TeamTalkBotConnection(server, config.teamtalk, config.bot, star_pool, control)
        for server in config.servers
        if server.enabled
    ]
    if not connections:
        print("All TeamTalk server sections are disabled.")
        star_pool.stop()
        return 2

    for connection in connections:
        connection.start()

    print("STAR TeamTalk bot is running. Press Ctrl+C to stop.")
    try:
        while not control.stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        for connection in connections:
            connection.stop()
        star_pool.stop()
        for connection in connections:
            connection.join(timeout=5)
        star_pool.join(timeout=2)
    return EXIT_RESTART if control.restart_requested() else 0


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
