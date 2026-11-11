# tt-star

A headless TeamTalk user client that turns private messages into speech using
a STAR coagulator and speaks it in the bot's channel.

Speech is sent as the bot's voice stream (TeamTalk's `TT_InsertAudioBlock`),
like a person talking: it is decoded with ffmpeg to the channel codec's
sample rate and channels, and listeners set its volume as the bot's voice
volume. The bot's account needs the right to transmit voice in its channel.
`tests/audio_smoothness_test.py` measures how it plays at a listener.

## Requirements

- Python 3.10 or newer
- A STAR coagulator WebSocket endpoint
- ffmpeg (to decode the synthesized audio)
- The separately licensed TeamTalk 5 SDK

Install the Python dependencies:

```console
python -m pip install -r requirements.txt
```

Copy `config.example.ini` to a private location, edit it, and restrict its
permissions because it contains server and SDK credentials. You can also use
the interactive configurator:

```console
python bot.py --config ~/.config/tt-star/config.ini --configure
```

Then run the bot:

```console
python bot.py --config ~/.config/tt-star/config.ini
```

On Windows, use a path such as
`%LOCALAPPDATA%\tt-star\config.ini` instead.

## Chat commands

Send the bot a private message. Anything that is not a command is spoken in
the bot's channel with your chosen voice. `help` lists the commands. Two of
them affect only the server they are sent on; the bot stays connected to
every other server:

- `restart` reconnects the bot to this server, with its settings read again
  from the configuration file.
- `quit` disconnects the bot from this server until the bot is restarted (or
  it is connected again on the web page).

Restart the whole bot with your service manager or on the web page.

## Web interface

The bot can serve a management page from inside its own process: every
server's state, who is in its channel and what is being spoken; turning
servers on and off, reconnecting and disconnecting them; adding servers
(paste a `tt://` link to fill in the form), editing and removing them, all
while the other servers keep running; speaking text in a channel; each
person's voice, rate and pitch choices; the voices on offer; the bot-wide
settings; the log; and restarting the bot. Turn it on in the configuration:

```ini
[web]
enabled = yes
host = 127.0.0.1
port = 8776
```

It has **no password**. Keep it on 127.0.0.1 and put a reverse proxy in front
of it that admits only trusted addresses (for example only your Tailscale
network). The proxy must pass the `Host` header with its port, because
changes are refused when their `Origin` does not match it. Changes also need
the `X-STAR-Request` header, which other sites cannot send.

Changes made on the page are written to the configuration file (readable by
its owner only, since it holds passwords).

`tests/live_web_test.py` tests the commands and the page end to end against
two throwaway TeamTalk servers and a STAR coagulator; see its docstring.

## TeamTalk SDK setup

If TeamTalk is already importable, the bot uses that installation. You can
also set `TEAMTALK_SDK_DIR` to an unpacked TeamTalk SDK directory.

Otherwise, the bot detects Windows, macOS, Linux x86-64, or Linux arm64;
discovers the latest matching Standard SDK on BearWare's live download page;
shows the applicable trial and licensing notice; and asks before downloading.
The SDK is installed only for the current user:

- Linux: `${XDG_DATA_HOME:-~/.local/share}/tt-star/teamtalk-sdk`
- macOS: `~/Library/Application Support/tt-star/teamtalk-sdk`
- Windows: `%LOCALAPPDATA%\tt-star\teamtalk-sdk`

BearWare currently describes downloads as 30-day trials and requires a paid
license for end-user applications. Each contributor is responsible for their
own SDK entitlement. See [THIRD_PARTY.md](THIRD_PARTY.md).

## Security and privacy

Private messages sent to the bot are forwarded to the configured STAR server
for synthesis. Use `wss://` for non-local endpoints and do not commit a live
configuration file. At present, run the bot only on a trusted TeamTalk server:
administrative chat commands are not yet restricted to an operator allowlist
(`restart` and `quit` affect only the server they are sent on).

## License

The original code in this repository is licensed under the MIT License. The
TeamTalk SDK is a separate proprietary dependency and is not covered by that
license.
