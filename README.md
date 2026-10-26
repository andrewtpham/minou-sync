# minou-sync

Own your training history and unlock findings with your personal AI.

minou-sync gathers every workout from Garmin, Strava and other apps into one SQLite file on your computer. Daily Garmin wellness is brought in: sleep, HRV, resting heart rate, Body Battery and weight to help you understand your recovery. An AI agent reads the file through read-only tools and answers from your own numbers. The tools are small enough for a model running on your laptop so your health data can stay on your machine.

![A local model answering questions about made-up training data through minou-sync](docs/demo/ask.gif)

## Why minou-sync

- **Every platform in one place.** Years of Garmin and Strava plus exports from Nike Run Club, COROS, Polar, intervals.icu or your own spreadsheet. A workout recorded on two platforms appears once.
- **Records from your original files.** Best efforts come from the FIT, GPX and TCX files of every device you've used. GPS jumps and impossible values are dropped so a glitch can't set a record.
- **Built for small local models.** The server does the counting, summing and date arithmetic. A model the size of Gemma 4 E2B can answer at an 8k context and nothing leaves your computer.
- **No accounts or API keys.** You sign in to Garmin once. Strava comes in from the archive Strava emails you. Nothing is hosted.
- **Stays current on its own.** A daily sync on your Mac pulls in new activities and tells you what arrived. A second note gives Garmin's readiness and recovery for the day.

It's tested with Claude in Claude Code and with Gemma 4 (`gemma4:e2b`) running locally through Ollama. Other MCP clients and models that can call tools should work too.

## Questions you can ask

- "How much have I run year to date, distance and elevation?"
- "Sessions by training effect in the last 8 weeks against the 8 before."
- "HRV, sleep and Body Battery in the two days after my five hardest sessions this year."
- "I have a 50 km trail race in six weeks. Based on the last three months, where are the gaps?"

[docs/getting-started.md](docs/getting-started.md#ask-your-questions) has more examples and a system prompt to start with.

## Quick start

You need macOS and a Garmin Connect account (MFA is supported). Linux should work but hasn't been tested. Your Strava archive is optional and adds activities from before you used Garmin.

Install with [pipx](https://pipx.pypa.io). It needs Python 3.12 or later.

```sh
brew install pipx
pipx ensurepath        # once; then open a new terminal
pipx install git+https://github.com/andrewtpham/minou-sync.git
```

[uv](https://docs.astral.sh/uv/) works too: `uv tool install git+https://github.com/andrewtpham/minou-sync.git` fetches Python for you if needed. `pipx upgrade minou-sync` updates it. `pipx uninstall minou-sync` removes the program and leaves your data.

Then build your archive:

```sh
minou-sync login       # sign in to Garmin, once
minou-sync backfill    # pull your history; rerun until "Backfill complete."
minou-sync schedule    # sync every day at 09:00 and notify you when there's something new
minou-sync status      # counts, date range, data folder and archive size
```

The first command asks where to keep your data. Press Enter for `~/.local/share/minou-sync` or give another folder. For the daily sync avoid Desktop, Documents, Downloads, iCloud Drive and external drives. macOS asks permission before a program reads those and a scheduled job can't answer.

Data from other platforms comes in with `import`. Here the CLI imports a made-up activities CSV and a wellness CSV, merges them and reports what the archive holds:

![The minou-sync CLI importing, reconciling and reporting status on made-up data](docs/demo/cli.gif)

Then connect an agent. For Claude Code run `claude mcp add minou-sync -- minou-sync mcp`. [docs/getting-started.md](docs/getting-started.md) walks through every step and covers Kiro, Claude Desktop, Cursor, Open WebUI and local models through Ollama.

## Docs

- [Getting started](docs/getting-started.md): from install to your first question, with every command and tool.
- [How it works](docs/how-it-works.md): the sources, how they merge and what the archive holds.
- [Importing from any platform](docs/import.md): profiles and CSV templates.

## Your data and privacy

- Everything stays in one data folder on your computer (`~/.local/share/minou-sync` unless you choose another). The folder is owner-only. minou-sync warns you if the folder is inside a git repository so your health data isn't committed by accident.
- Garmin session tokens are saved in `~/.garminconnect` with owner-only permissions. Treat that folder like a password. Your Garmin password isn't stored.
- The only network traffic is minou-sync's own requests to Garmin.
- When you ask an AI agent about your data the rows it reads go to that agent's model provider. With a local model through Ollama they stay on your computer.
- minou-sync is a data tool. It doesn't give medical advice.

## Limits

- Garmin sync uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library. If Garmin changes its sign-in or its API sync can break until the library is updated. Garmin's account export still works in the meantime.
- The watch's all-day monitoring files and ECG recordings aren't imported.
- Garmin's HRV status (balanced, unbalanced) isn't in the account export. Days that come only from the export have the HRV average without the status.
- So far it has been tested with one Garmin and Strava account.
- A small local model answers most single questions well. Questions that need several steps are less reliable on it.

## Development

```sh
git clone https://github.com/andrewtpham/minou-sync.git
cd minou-sync
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip   # --group needs pip 25.1 or later
.venv/bin/pip install -e . --group dev
.venv/bin/pytest -q
```

The tests never touch your data folder, settings or daily sync. `.venv/bin/minou-sync` runs your working copy against the same data folder as the installed command. To have plain `minou-sync` run your working copy install it with `pipx install -e .`.

## Status

An early working version.

## License and contributing

minou-sync is released under the [MIT License](LICENSE). Use it freely, including commercially.

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
