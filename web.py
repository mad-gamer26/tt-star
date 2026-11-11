"""Management web interface for the STAR TeamTalk bot, served from inside the
bot process.

It has no password of its own. Bind it to 127.0.0.1 and put it behind a proxy
that admits only trusted networks (here: nginx, Tailscale addresses only).
Requests that change anything must carry the X-STAR-Request header, which a
page on another site cannot send without the browser asking first, and an
Origin, if present, must match the host.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import re
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

import bot as star_bot
import botlog
from botlog import log

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 256 * 1024
MAX_SPEAK = 4000
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
NAME = r"([A-Za-z0-9][A-Za-z0-9._-]*)"

STATE_TEXT = {
    "connecting": "Connecting",
    "logging_in": "Connected, logging in",
    "joining": "Logged in, joining the channel",
    "connected": "In the channel",
    "reconnecting": "Disconnected, trying again",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ state


def server_status(manager: star_bot.BotManager, server: star_bot.TeamTalkServerSettings) -> dict[str, Any]:
    connection = manager.connections.get(server.name)
    status: dict[str, Any] = {
        "name": server.name,
        "enabled": server.enabled,
        "running": connection is not None,
        "host": f"{server.host}:{server.tcp_port}",
        "encrypted": server.encrypted,
        "nickname": server.nickname,
        "username": server.username,
        "channel": server.channel,
        "started_at": connection.started_at if connection else None,
        "channel_path": "",
        "others": None,
        "queue": 0,
        "speaking": "",
        "can_speak": None,
    }
    if connection is None:
        if not server.enabled:
            status["state"], status["state_text"] = "off", "Turned off"
        elif server.name in manager.not_running:
            status["state"] = "stopped"
            status["state_text"] = "Not connected: " + manager.not_running[server.name]
        else:
            status["state"], status["state_text"] = "stopped", "Not connected"
        return status
    status["state"] = connection.state
    status["state_text"] = STATE_TEXT.get(connection.state, connection.state)
    if connection.state == "no_channel":
        status["state_text"] = f"Logged in, but there is no channel {server.channel} on this server"
    try:
        status.update(connection.call(connection.snapshot, timeout=2))
    except Exception:
        pass  # connecting, or busy: the basic values above are enough
    return status


def _settings_from(data: dict, cls, base=None):
    """Builds a settings dataclass from JSON, keeping `base`'s values for
    anything missing and checking each value's type."""
    base = base or cls(**({"name": "", "host": ""} if cls is star_bot.TeamTalkServerSettings else {}))
    values = dataclasses.asdict(base)
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        current = values[f.name]
        label = f.name.replace("_", " ")
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"{label} must be true or false.")
        elif isinstance(current, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise HttpError(HTTPStatus.BAD_REQUEST, f"The {label} must be a whole number.")
            value = int(value)
        elif isinstance(current, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"The {label} must be a number.")
            value = float(value)
        elif isinstance(current, list):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"The {label} must be a list of text lines.")
            value = [item.strip() for item in value if item.strip()]
        else:
            if not isinstance(value, str):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"The {label} must be text.")
            if "\n" in value or "\r" in value:
                raise HttpError(HTTPStatus.BAD_REQUEST, f"The {label} must be on one line.")
        values[f.name] = value
    return cls(**values)


# --------------------------------------------------------------- server


class WebInterface:
    def __init__(self, manager: star_bot.BotManager, host: str, port: int) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self.httpd = ThreadingHTTPServer((host, port), self._handler_class())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self.routes: list[tuple[str, re.Pattern, Callable[..., Any]]] = []
        self._register_routes()

    def start(self) -> None:
        self.thread.start()
        log(f"Web interface listening on http://{self.host}:{self.port}")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # routes -------------------------------------------------------------

    def _route(self, method: str, pattern: str, function: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), function))

    def _register_routes(self) -> None:
        servers = "/api/servers/" + NAME
        self._route("GET", "/api/state", self.api_state)
        self._route("POST", "/api/servers", self.api_add_server)
        self._route("GET", servers, self.api_get_server)
        self._route("PUT", servers, self.api_update_server)
        self._route("DELETE", servers, self.api_remove_server)
        self._route("POST", servers + "/enabled", self.api_set_enabled)
        self._route("POST", servers + "/restart", self.api_restart_server)
        self._route("POST", servers + "/disconnect", self.api_disconnect_server)
        self._route("POST", servers + "/stop", self.api_stop_speaking)
        self._route("POST", servers + "/speak", self.api_speak)
        self._route("GET", servers + "/people", self.api_people)
        self._route("DELETE", servers + "/people/(.+)", self.api_forget_person)
        self._route("GET", "/api/voices", self.api_voices)
        self._route("GET", "/api/logs", self.api_logs)
        self._route("GET", "/api/settings", self.api_get_settings)
        self._route("PUT", "/api/settings", self.api_update_settings)
        self._route("POST", "/api/process/restart", self.api_restart_process)

    # helpers --------------------------------------------------------------

    def _server(self, name: str) -> star_bot.TeamTalkServerSettings:
        try:
            return self.manager.server(name)
        except star_bot.ServerError as exc:
            raise HttpError(HTTPStatus.NOT_FOUND, str(exc))

    def _connection(self, name: str) -> star_bot.TeamTalkBotConnection:
        server = self._server(name)
        connection = self.manager.connections.get(name)
        if connection is None:
            hint = "Turn it on first." if not server.enabled else "Connect it first."
            raise HttpError(HTTPStatus.CONFLICT, f"The bot is not connected to {name}. {hint}")
        return connection

    def _on_connection(self, name: str, function: Callable[[star_bot.TeamTalkBotConnection], Any]) -> Any:
        connection = self._connection(name)
        try:
            return connection.call(lambda: function(connection), timeout=5)
        except (RuntimeError, TimeoutError) as exc:
            raise HttpError(HTTPStatus.CONFLICT, f"{name}: {exc}.")

    def _status(self, name: str) -> dict[str, Any]:
        return server_status(self.manager, self._server(name))

    # API ------------------------------------------------------------------

    def api_state(self, query, body):
        pool = self.manager.star_pool
        return {
            "servers": [server_status(self.manager, server) for server in list(self.manager.config.servers)],
            "star": {"coagulators": pool.status(), "voices": len(pool.all_voices())},
            "process": {
                "pid": os.getpid(),
                "started_at": self.manager.process_started_at,
                "sdk": str(star_bot.tt.ttstr(star_bot.tt.getVersion())),
            },
            "time": time.time(),
        }

    def api_get_server(self, query, body, name):
        server = self._server(name)
        return {"config": dataclasses.asdict(server), "status": server_status(self.manager, server)}

    def api_add_server(self, query, body):
        config = body.get("config")
        if not isinstance(config, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "No settings were sent.")
        settings = _settings_from(config, star_bot.TeamTalkServerSettings)
        settings.name = settings.name.strip()
        settings.host = settings.host.strip()
        try:
            self.manager.add_server(settings)
        except star_bot.ServerError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc))
        message = f"Server {settings.name} added."
        message += " The bot is connecting to it." if settings.enabled else " It is turned off."
        return {"message": message, "status": self._status(settings.name)}

    def api_update_server(self, query, body, name):
        current = self._server(name)
        config = body.get("config")
        if not isinstance(config, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "No settings were sent.")
        settings = _settings_from(config, star_bot.TeamTalkServerSettings, base=current)
        settings.host = settings.host.strip()
        try:
            running = self.manager.update_server(name, settings)
        except star_bot.ServerError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc))
        message = f"Settings saved. The bot is reconnecting to {name} with them." if running else (
            f"Settings saved. {name} is turned off."
        )
        return {"message": message, "status": self._status(name)}

    def api_remove_server(self, query, body, name):
        self._server(name)
        self.manager.remove_server(name)
        return {"message": f"Server {name} removed. The bot left it and its settings were deleted."}

    def api_set_enabled(self, query, body, name):
        self._server(name)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HttpError(HTTPStatus.BAD_REQUEST, "enabled must be true or false.")
        self.manager.set_enabled(name, enabled)
        message = f"{name} turned on. The bot is connecting to it." if enabled else (
            f"{name} turned off. The bot left it and will not connect to it until it is turned on."
        )
        return {"message": message, "status": self._status(name)}

    def api_restart_server(self, query, body, name):
        server = self._server(name)
        if not server.enabled:
            raise HttpError(HTTPStatus.CONFLICT, f"{name} is turned off. Turn it on instead.")
        was_running = name in self.manager.connections
        try:
            self.manager.reload_and_restart(name)
        except star_bot.ServerError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc))
        message = f"Reconnecting to {name}." if was_running else f"Connecting to {name}."
        return {"message": message + " Its settings were read again from the configuration file.",
                "status": self._status(name)}

    def api_disconnect_server(self, query, body, name):
        self._connection(name)
        self.manager._stop(name, reason="disconnected from the web page")
        return {"message": f"Disconnected from {name} until the bot restarts or you connect it again. "
                           "It stays turned on.", "status": self._status(name)}

    def api_stop_speaking(self, query, body, name):
        count = self._on_connection(name, lambda c: c.stop_speaking())
        message = "Nothing was playing." if not count else (
            "Stopped speaking." if count == 1 else f"Stopped speaking and cleared {count - 1} waiting.")
        return {"message": message, "status": self._status(name)}

    def api_speak(self, query, body, name):
        text = str(body.get("text", "")).strip()
        voice = str(body.get("voice", "")).strip() or self.manager.config.bot.default_voice
        if not text:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Type what the bot should say first.")
        if len(text) > MAX_SPEAK:
            raise HttpError(HTTPStatus.BAD_REQUEST, f"That is too long; at most {MAX_SPEAK} characters.")
        if not voice:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Choose a voice first (there is no default voice).")
        message = self._on_connection(name, lambda c: c.speak_from_web(voice, text))
        log(f"Web page: speaking with {voice}: {text[:120]}", server=name)
        return {"message": message, "status": self._status(name)}

    def api_people(self, query, body, name):
        return {"people": self._on_connection(name, lambda c: c.people())}

    def api_forget_person(self, query, body, name, key):
        key = unquote(key)
        if not self._on_connection(name, lambda c: c.forget_person(key)):
            raise HttpError(HTTPStatus.NOT_FOUND, f"{key} has no choices on {name}.")
        log(f"Web page: forgot {key}'s voice, rate, pitch and names choices", server=name)
        return {"message": f"Forgot {key}'s choices on {name}. They get the default voice again."}

    def api_voices(self, query, body):
        text = query.get("q", [""])[0].strip().lower()
        voices = self.manager.star_pool.all_voices()
        found = [voice for voice in voices if text in voice.lower()] if text else voices
        return {"voices": found, "total": len(voices), "default": self.manager.config.bot.default_voice}

    def api_logs(self, query, body):
        def one(key: str, default: str = "") -> str:
            return query.get(key, [default])[0]

        server = one("server") or None
        bot_only = server == "_bot"
        try:
            after = int(one("after", "0"))
            limit = max(1, min(1000, int(one("limit", "300"))))
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "after and limit must be numbers.")
        records = botlog.buffer.records(
            after=after,
            server=None if bot_only else server,
            bot_only=bot_only,
            min_level=one("level", "INFO").upper(),
            limit=limit,
        )
        return {"records": records}

    def api_get_settings(self, query, body):
        config = self.manager.config
        return {
            "star": dataclasses.asdict(config.star),
            "bot": dataclasses.asdict(config.bot),
            "teamtalk": dataclasses.asdict(config.teamtalk),
            "web": dataclasses.asdict(config.web),
            "config_path": str(self.manager.config_path),
        }

    def api_update_settings(self, query, body):
        config = self.manager.config
        for key in ("star", "bot", "teamtalk"):
            if not isinstance(body.get(key), dict):
                raise HttpError(HTTPStatus.BAD_REQUEST, "No settings were sent.")
        star = _settings_from(body["star"], star_bot.StarSettings, base=config.star)
        bot = _settings_from(body["bot"], star_bot.BotSettings, base=config.bot)
        teamtalk = _settings_from(body["teamtalk"], star_bot.TeamTalkGlobalSettings, base=config.teamtalk)
        if not star.hosts:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Give at least one STAR coagulator address.")
        for host in star.hosts:
            if not re.match(r"^wss?://\S+$", host):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"{host} is not a websocket address (ws:// or wss://).")
        if star.request_timeout < 1:
            raise HttpError(HTTPStatus.BAD_REQUEST, "The synthesis timeout must be at least 1 second.")
        if teamtalk.reconnect_seconds < 1:
            raise HttpError(HTTPStatus.BAD_REQUEST, "The reconnect delay must be at least 1 second.")
        if bot.max_voice_results < 1:
            raise HttpError(HTTPStatus.BAD_REQUEST, "The number of voices to list must be at least 1.")
        if not bot.command_prefix.strip():
            raise HttpError(HTTPStatus.BAD_REQUEST, "The command prefix cannot be empty.")
        try:
            bot.voice_preview_text.format(voice="x")
        except (KeyError, IndexError, ValueError):
            raise HttpError(HTTPStatus.BAD_REQUEST, "The preview text may only use {voice} in curly braces.")
        changed_star = (star.hosts, star.request_timeout) != (config.star.hosts, config.star.request_timeout)
        needs_restart = self.manager.update_settings(star, bot, teamtalk)
        message = "Settings saved and in use."
        if changed_star:
            message += " The bot reconnected to the STAR coagulators."
        if needs_restart:
            message += " Restart the bot for " + " and ".join(needs_restart) + " to take effect."
        return {"message": message}

    def api_restart_process(self, query, body):
        log("Web page: restarting the whole bot")
        threading.Timer(0.3, self.manager.restart_process).start()
        return {"message": "The bot is restarting. Every server reconnects in a few seconds."}

    # handler ----------------------------------------------------------------

    def _handler_class(self):
        interface = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "STAR-Bot-Web"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, data: dict[str, Any]) -> None:
                self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

            def _check_request_origin(self) -> None:
                if self.headers.get("X-STAR-Request") != "1":
                    raise HttpError(HTTPStatus.FORBIDDEN, "Missing request header.")
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise HttpError(HTTPStatus.FORBIDDEN, "Requests from other sites are not allowed.")

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The request is too large.")
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    raise HttpError(HTTPStatus.BAD_REQUEST, "The request is not valid JSON.")
                if not isinstance(data, dict):
                    raise HttpError(HTTPStatus.BAD_REQUEST, "The request must be a JSON object.")
                return data

            def _handle(self) -> None:
                parts = urlsplit(self.path)
                path = parts.path
                try:
                    if path.startswith("/api/"):
                        method = self.command if self.command != "HEAD" else "GET"
                        if method != "GET":
                            self._check_request_origin()
                        for route_method, pattern, function in interface.routes:
                            match = pattern.match(path)
                            if match and route_method == method:
                                body = self._body() if method in ("POST", "PUT", "DELETE") else {}
                                groups = [unquote(group) for group in match.groups()]
                                result = function(parse_qs(parts.query), body, *groups)
                                self._json(HTTPStatus.OK, dict(result or {}, ok=True))
                                return
                        raise HttpError(HTTPStatus.NOT_FOUND, "Not found.")
                    if self.command not in ("GET", "HEAD"):
                        raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "Not allowed.")
                    self._page_or_static(unquote(path))
                except HttpError as exc:
                    if path.startswith("/api/"):
                        self._json(exc.status, {"ok": False, "error": str(exc)})
                    else:
                        self._send(exc.status, _page("Not found", "notfound", str(exc)).encode(), "text/html; charset=utf-8")
                except Exception as exc:
                    log(f"Web request failed: {self.command} {path}\n{traceback.format_exc().rstrip()}", level="ERROR")
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"Something went wrong: {exc}"})

            def _page_or_static(self, path: str) -> None:
                if path.startswith("/static/"):
                    file_name = path[len("/static/"):]
                    extension = os.path.splitext(file_name)[1]
                    full = os.path.join(STATIC_DIR, file_name)
                    if "/" in file_name or extension not in STATIC_TYPES or not os.path.isfile(full):
                        raise HttpError(HTTPStatus.NOT_FOUND, "Not found.")
                    with open(full, "rb") as f:
                        self._send(HTTPStatus.OK, f.read(), STATIC_TYPES[extension])
                    return
                pages = {
                    "/": ("Servers", "overview", ""),
                    "/add": ("Add a server", "add", ""),
                    "/voices": ("Voices", "voices", ""),
                    "/log": ("Log", "log", ""),
                    "/settings": ("Bot settings", "settings", ""),
                }
                if path in pages:
                    title, page, argument = pages[path]
                else:
                    match = re.match(r"^/servers/" + NAME + "$", path)
                    names = [server.name for server in interface.manager.config.servers]
                    if not match or match.group(1) not in names:
                        raise HttpError(HTTPStatus.NOT_FOUND, "There is no such page.")
                    title, page, argument = "Server " + match.group(1), "server", match.group(1)
                self._send(HTTPStatus.OK, _page(title, page, argument).encode(), "text/html; charset=utf-8")

            def do_GET(self) -> None:
                self._handle()

            def do_HEAD(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

        return Handler


NAV = [
    ("/", "Servers"),
    ("/add", "Add a server"),
    ("/voices", "Voices"),
    ("/log", "Log"),
    ("/settings", "Bot settings"),
]


def _page(title: str, page: str, argument: str) -> str:
    links = "".join(
        '<li><a href="{0}"{2}>{1}</a></li>'.format(
            href, html.escape(label), ' aria-current="page"' if label == title else ""
        )
        for href, label in NAV
    )
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - STAR bot</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/app.js" defer></script>
</head>
<body data-page="{page}" data-argument="{argument}">
<a class="skip" href="#main">Skip to main content</a>
<header>
<p class="brand">STAR TeamTalk bot</p>
<nav aria-label="Main"><ul>{links}</ul></nav>
</header>
<main id="main" tabindex="-1">
<h1>{heading}</h1>
<div id="status" role="status" aria-live="polite" aria-atomic="true"></div>
<div id="alert" role="alert" aria-atomic="true"></div>
<div id="content"><p>Loading…</p></div>
<noscript><p>This page needs JavaScript.</p></noscript>
</main>
</body>
</html>
""".format(
        title=html.escape(title),
        heading=html.escape(title),
        page=html.escape(page),
        argument=html.escape(argument),
        links=links,
    )
