"""Live test of the per-server /restart and /quit and of the web interface,
against throwaway TeamTalk servers (never real ones) and a STAR coagulator.

    TEAMTALK_SDK_DIR=... python tests/live_web_test.py

Environment (defaults in brackets):
  TEST_TT_PORTS     two throwaway servers on localhost [19311,19312]
  TEST_BOT_USER / TEST_BOT_PASSWORD    the bot's account on both [STAR / starpass1]
  TEST_USER / TEST_PASSWORD            a person's account on both [tester / testpass1]
  TEST_STAR         coagulator [ws://localhost:7774]
  TEST_WEB_PORT     the test bot's web interface [8777]

It starts its own copy of the bot with a temporary configuration, so the
running bot and its config.ini are not touched.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from teamtalk_sdk import load_teamtalk_sdk  # noqa: E402

tt = load_teamtalk_sdk()

PORTS = [int(p) for p in os.environ.get("TEST_TT_PORTS", "19311,19312").split(",")]
BOT_USER = os.environ.get("TEST_BOT_USER", "STAR")
BOT_PASSWORD = os.environ.get("TEST_BOT_PASSWORD", "starpass1")
USER = os.environ.get("TEST_USER", "tester")
PASSWORD = os.environ.get("TEST_PASSWORD", "testpass1")
STAR = os.environ.get("TEST_STAR", "ws://localhost:7774")
WEB_PORT = int(os.environ.get("TEST_WEB_PORT", "8777"))
BASE = f"http://127.0.0.1:{WEB_PORT}"
BOT_NICK = "STAR Test Bot"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append((name, bool(ok)))
    print(f"{name}: {'PASS' if ok else 'FAIL'}{'' if ok or detail == '' else '  (' + str(detail)[-800:] + ')'}", flush=True)
    return bool(ok)


def wait_for(predicate, timeout: float = 30, every: float = 0.3) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(every)
    return False


def request(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"X-STAR-Request": "1", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            ctype = response.headers.get("Content-Type", "")
            return response.status, (json.loads(raw) if "json" in ctype else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def api(method: str, path: str, body=None):
    status, data = request(method, path, body)
    if status != 200:
        raise AssertionError(f"{method} {path} -> {status}: {data}")
    return data


def state() -> dict:
    return api("GET", "/api/state")


def server_state(name: str) -> dict:
    return next(s for s in state()["servers"] if s["name"] == name)


class Person(tt.TeamTalk, threading.Thread):
    """A TeamTalk user who messages the bot and watches it stream."""

    def __init__(self, port: int):
        tt.TeamTalk.__init__(self)
        threading.Thread.__init__(self, daemon=True)
        self.port = port
        self.messages: list[str] = []
        self.ready = threading.Event()
        self.stop_flag = False
        self.actions: list = []
        self.lock = threading.Lock()
        self.saw_stream = False
        self.streaming = False
        self.my_id = 0

    def run(self) -> None:
        self.connect(tt.ttstr("127.0.0.1"), self.port, self.port)
        while not self.stop_flag:
            with self.lock:
                actions, self.actions = self.actions, []
            for action in actions:
                action()
            self.runEventLoop(100)
        self.disconnect()

    def do(self, function):
        with self.lock:
            self.actions.append(function)

    def onConnectSuccess(self):
        # A sound output (TeamTalk's virtual one), so the client receives
        # the bot's media stream and reports it in the bot's user state.
        self.initSoundOutputDevice(tt.TT_SOUNDDEVICE_ID_TEAMTALK_VIRTUAL)
        self.doLogin(tt.ttstr("Tester"), tt.ttstr(USER), tt.ttstr(PASSWORD), tt.ttstr("live test"))

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        self.doJoinChannelByID(self.getRootChannelID(), tt.ttstr(""))

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self.my_id:
            self.ready.set()

    def onCmdUserTextMessage(self, message):
        # (The server delivers private messages with nToUserID 0.)
        if message.nMsgType == tt.TextMsgType.MSGTYPE_USER and message.nFromUserID != self.my_id:
            self.messages.append(str(tt.ttstr(message.szMessage)))

    def onUserStateChange(self, user):
        streaming = bool(user.uUserState & tt.UserState.USERSTATE_MEDIAFILE_AUDIO)
        if streaming and not self.streaming:
            self.saw_stream = True
        self.streaming = streaming

    def bot_id(self) -> int:
        for user in self.getServerUsers():
            if str(tt.ttstr(user.szNickname)) == BOT_NICK:
                return user.nUserID
        return 0

    def say(self, text: str) -> None:
        bot = self.bot_id()
        assert bot, "the bot is not on the server"

        def send():
            for message in tt.buildTextMessage(text, tt.TextMsgType.MSGTYPE_USER, nToUserID=bot, nFromUserID=self.my_id):
                self.doTextMessage(message)

        self.do(send)

    def reply_containing(self, text: str, timeout: float = 15) -> str | None:
        found: list[str] = []
        wait_for(lambda: found.extend(m for m in self.messages if text in m) or found, timeout)
        return found[0] if found else None


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="star-web-test-"))
    config = work / "config.ini"
    servers = "".join(
        f"\n[server:t{i + 1}]\nenabled = yes\nhost = 127.0.0.1\ntcp_port = {port}\nudp_port = {port}\n"
        f"encrypted = no\nnickname = {BOT_NICK}\nusername = {BOT_USER}\npassword = {BOT_PASSWORD}\n"
        f"channel = /\nchannel_password = \n"
        for i, port in enumerate(PORTS)
    )
    config.write_text(
        f"[star]\nhosts = {STAR}\nrequest_timeout = 60\n\n[bot]\ncommand_prefix = /\ndefault_voice = \n"
        "voice_preview_text = Hello there, my name is {voice}.\nmax_voice_results = 20\n"
        f"temp_dir = {work}/speech\n\n[teamtalk]\nclient_name = STAR TeamTalk Bot\nreconnect_seconds = 2\n"
        f"license_name = \nlicense_key = \n\n[web]\nenabled = yes\nhost = 127.0.0.1\nport = {WEB_PORT}\n" + servers
    )
    log_file = open(work / "bot.log", "w")
    bot = subprocess.Popen([sys.executable, str(ROOT / "bot.py"), "--config", str(config)],
                           stdout=log_file, stderr=subprocess.STDOUT, cwd=ROOT)
    people: list[Person] = []
    try:
        check("the web interface answers", wait_for(lambda: state(), 20))
        check("both servers join their channel", wait_for(
            lambda: all(s["state"] == "connected" for s in state()["servers"]), 30), state())
        check("the STAR coagulator is connected and has voices", wait_for(
            lambda: state()["star"]["voices"] > 0, 30), state()["star"])
        pid = bot.pid
        started = state()["process"]["started_at"]

        # Pages and static files.
        for path, ok_status in [("/", 200), ("/add", 200), ("/voices", 200), ("/log", 200), ("/settings", 200),
                                ("/servers/t1", 200), ("/servers/nope", 404), ("/static/app.js", 200),
                                ("/static/style.css", 200), ("/static/../web.py", 404)]:
            status, _ = request("GET", path)
            check(f"GET {path} is {ok_status}", status == ok_status, status)

        # Changes need the header, and no other site's Origin.
        status, _ = request("POST", "/api/servers/t1/stop", {}, headers={"X-STAR-Request": ""})
        check("a change without the request header is refused", status == 403, status)
        status, _ = request("POST", "/api/servers/t1/stop", {}, headers={"Origin": "http://evil.example"})
        check("a change from another site is refused", status == 403, status)

        # A person on t1 talks to the bot.
        person = Person(PORTS[0])
        people.append(person)
        person.start()
        check("the test person joins t1", person.ready.wait(15))
        wait_for(lambda: person.bot_id(), 10)
        person.say("/status")
        check("/status answers", person.reply_containing("STAR connections"))

        voices = api("GET", "/api/voices")["voices"]
        voice = voices[0]
        person.say(f"/voice {voice}")
        check("/voice chooses a voice", person.reply_containing("Voice set to"))
        person.say("/rate 1")
        person.reply_containing("Rate set to")
        people_list = api("GET", "/api/servers/t1/people")["people"]
        check("the web page lists the person's choices", any(p["key"] == USER and p["voice"] == voice and p["rate"] == "1"
                                                            for p in people_list), people_list)

        # Speaking: from the person, then from the web page.
        person.saw_stream = False
        person.say("hello from the live test")
        check("a private message is spoken in the channel", wait_for(lambda: person.saw_stream, 60), person.messages[-3:])
        wait_for(lambda: not person.streaming, 60)  # the first one has finished
        person.saw_stream = False
        result = api("POST", "/api/servers/t1/speak", {"voice": voice, "text": "Hello from the web page."})
        check("Say something on the web page is accepted", "Synthesizing" in result["message"], result)
        check("...and spoken in the channel", wait_for(lambda: person.saw_stream, 60))
        status, data = request("POST", "/api/servers/t1/speak", {"voice": voice, "text": ""})
        check("empty text is refused with a message", status == 400 and "Type what" in data["error"], data)

        # /restart: only t1 reconnects; t2 and the process are untouched.
        t1_before = server_state("t1")["started_at"]
        t2_before = server_state("t2")["started_at"]
        person.messages.clear()
        person.say("/restart")
        check("/restart answers", person.reply_containing("Reconnecting to this server"))
        check("/restart reconnects t1", wait_for(lambda: server_state("t1")["started_at"] != t1_before
                                                 and server_state("t1")["state"] == "connected", 30), server_state("t1"))
        check("...t2 is not touched", server_state("t2")["started_at"] == t2_before
              and server_state("t2")["state"] == "connected", server_state("t2"))
        check("...the bot process keeps running", bot.poll() is None and state()["process"]["started_at"] == started)
        check("...t1's settings are read again (people's choices start over)",
              api("GET", "/api/servers/t1/people")["people"] == [])

        # /quit: only t1 disconnects.
        wait_for(lambda: person.bot_id(), 10)
        person.messages.clear()
        person.say("/quit")
        check("/quit answers", person.reply_containing("Disconnecting from this server"))
        check("/quit disconnects t1 only", wait_for(lambda: server_state("t1")["state"] == "stopped", 15), server_state("t1"))
        check("...saying why", "/quit" in server_state("t1")["state_text"], server_state("t1")["state_text"])
        check("...t1 stays turned on", server_state("t1")["enabled"])
        check("...t2 is still connected", server_state("t2")["state"] == "connected" and server_state("t2")["started_at"] == t2_before)
        check("...the bot process keeps running", bot.poll() is None)
        check("...the bot left t1", wait_for(lambda: not person.bot_id(), 10))

        # The web page connects it again.
        result = api("POST", "/api/servers/t1/restart")
        check("Connect on the web page", "Connecting to t1" in result["message"], result)
        check("...t1 is back", wait_for(lambda: server_state("t1")["state"] == "connected", 30), server_state("t1"))
        check("...and others in its channel are listed", wait_for(lambda: "Tester" in (server_state("t1")["others"] or []), 10),
              server_state("t1"))

        # Disconnect from the web page, then Turn off / on.
        result = api("POST", "/api/servers/t2/disconnect")
        check("Disconnect on the web page", server_state("t2")["state"] == "stopped", result)
        result = api("POST", "/api/servers/t2/restart")
        check("...Connect again", wait_for(lambda: server_state("t2")["state"] == "connected", 30))
        api("POST", "/api/servers/t2/enabled", {"enabled": False})
        check("Turn off", server_state("t2")["state"] == "off")
        check("...saved in the file", "[server:t2]\nenabled = no" in config.read_text())
        api("POST", "/api/servers/t2/enabled", {"enabled": True})
        check("Turn on", wait_for(lambda: server_state("t2")["state"] == "connected", 30))

        # Add, edit and remove a server while the others keep running.
        t1_started = server_state("t1")["started_at"]
        new = {"name": "t3", "host": "127.0.0.1", "tcp_port": PORTS[1], "udp_port": PORTS[1], "encrypted": False,
               "nickname": "STAR Test Bot 3", "username": BOT_USER, "password": BOT_PASSWORD, "channel": "/",
               "channel_password": "", "enabled": True}
        result = api("POST", "/api/servers", {"config": new})
        check("Add a server", "added" in result["message"], result)
        check("...it connects", wait_for(lambda: server_state("t3")["state"] == "connected", 30), server_state("t3"))
        status, data = request("POST", "/api/servers", {"config": new})
        check("...a second server with the same name is refused", status == 400 and "already" in data["error"], data)
        bad = dict(new, name="bad name!")
        status, data = request("POST", "/api/servers", {"config": bad})
        check("...a name with spaces is refused", status == 400, data)
        status, data = request("POST", "/api/servers", {"config": dict(new, name="t4", tcp_port=70000)})
        check("...a port out of range is refused", status == 400 and "TCP port" in data["error"], data)
        edited = dict(new, nickname="STAR Test Bot Three")
        result = api("PUT", "/api/servers/t3", {"config": edited})
        check("Edit a server", wait_for(lambda: server_state("t3")["nickname"] == "STAR Test Bot Three"
                                        and server_state("t3")["state"] == "connected", 30), server_state("t3"))
        api("PUT", "/api/servers/t3", {"config": dict(edited, channel="/No Such Channel/")})
        check("A channel that does not exist is reported in words", wait_for(
            lambda: "no channel /No Such Channel/" in server_state("t3")["state_text"], 30), server_state("t3"))
        api("DELETE", "/api/servers/t3")
        check("Remove a server", all(s["name"] != "t3" for s in state()["servers"]) and "[server:t3]" not in config.read_text())
        check("...t1 was not touched by any of it", server_state("t1")["started_at"] == t1_started)

        # Bot settings.
        settings = api("GET", "/api/settings")
        settings["bot"]["default_voice"] = voice
        settings["bot"]["voice_preview_text"] = "Testing {voice}."
        result = api("PUT", "/api/settings", {k: settings[k] for k in ("star", "bot", "teamtalk")})
        check("Save bot settings", "saved" in result["message"], result)
        text = config.read_text()
        check("...written to the file", f"default_voice = {voice}" in text and "Testing {voice}." in text)
        check("...readable by its owner only", stat.S_IMODE(os.stat(config).st_mode) == 0o600, oct(os.stat(config).st_mode))
        wait_for(lambda: person.bot_id(), 10)
        person.messages.clear()
        person.say("/current")
        check("...in use at once (default voice)", person.reply_containing(f"Current voice: {voice}"))
        settings["bot"]["voice_preview_text"] = "Bad {name}"
        status, data = request("PUT", "/api/settings", {k: settings[k] for k in ("star", "bot", "teamtalk")})
        check("...a preview text with an unknown {name} is refused", status == 400, data)
        settings["bot"]["voice_preview_text"] = "Testing {voice}."
        settings["star"]["hosts"] = ["not a websocket"]
        status, data = request("PUT", "/api/settings", {k: settings[k] for k in ("star", "bot", "teamtalk")})
        check("...a coagulator address that is not ws:// is refused", status == 400, data)

        # Log.
        records = api("GET", "/api/logs?server=t1")["records"]
        check("the log has t1's entries", any("/quit" in r["message"] for r in records) and all(r["server"] == "t1" for r in records))
        bot_records = api("GET", "/api/logs?server=_bot")["records"]
        check("...and the bot's own", bot_records and all(r["server"] is None for r in bot_records))

        # Restart the whole bot from the web page.
        result = api("POST", "/api/process/restart")
        check("Restart the whole bot", "restarting" in result["message"], result)
        check("...it comes back (same process, started again)", wait_for(
            lambda: state()["process"]["started_at"] != started, 30))
        check("...still the same process for startman", bot.poll() is None and bot.pid == pid)
        check("...both servers reconnect", wait_for(
            lambda: all(s["state"] == "connected" for s in state()["servers"]), 30), state())
    except Exception as exc:
        import traceback
        check("the test ran to the end", False, traceback.format_exc())
    finally:
        for person in people:
            person.stop_flag = True
        bot.send_signal(signal.SIGINT)
        try:
            bot.wait(15)
        except subprocess.TimeoutExpired:
            bot.kill()
        log_file.close()
        print("\n--- bot output (last 40 lines) ---")
        print("\n".join((work / "bot.log").read_text().splitlines()[-40:]))
        leftovers = list((work / "speech").glob("*")) if (work / "speech").exists() else []
        check("no speech files are left behind", not leftovers, leftovers)
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    for name in failed:
        print("FAILED:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
