# tt-star

A headless TeamTalk user client that turns private messages into speech using
a STAR coagulator and streams the generated audio into the bot's channel.

## Requirements

- Python 3.10 or newer
- A STAR coagulator WebSocket endpoint
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
administrative chat commands are not yet restricted to an operator allowlist.

## License

The original code in this repository is licensed under the MIT License. The
TeamTalk SDK is a separate proprietary dependency and is not covered by that
license.
