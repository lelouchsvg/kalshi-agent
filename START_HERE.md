# Start here (zeke)

The agent runs on your Mac. You don't install anything yourself: the first time you start
it, it downloads a private copy of Python into this folder (about 60 MB, one time). Nothing
is installed system-wide, and deleting this folder removes all of it.

## Start it
Double-click **Start Kalshi Agent.command** in this folder.

- The first run takes a few minutes (download + safety tests). After that it starts in seconds.
- Your browser opens the dashboard at **http://127.0.0.1:8080**. Bookmark it.
- You can close the Terminal window; the agent keeps running.

If macOS says the file "can't be opened" or "doesn't have access privileges", open
**Terminal** (Applications → Utilities → Terminal), paste this line and press Enter:
```
bash ~/kalshi-agent/start.sh
```

## Stop it
Double-click **Stop Kalshi Agent.command** (or in Terminal: `bash ~/kalshi-agent/stop.sh`).
For an emergency stop of trading only, press the red **KILL SWITCH** on the dashboard.

## Things to know about running on a laptop
- **It only works while the Mac is awake.** The agent keeps the Mac from idle-sleeping while
  it runs, but closing the lid still puts it to sleep. Keep it plugged in with the lid open.
- **Sleep and Wi-Fi drops are handled safely.** When data goes stale, the health check fails
  and the agent will not trade (later phases), then it resumes on its own.
- **Gaps in data are normal.** Hours the Mac was off are simply missing from the research
  data; the models will be trained only on what was actually recorded.
- It uses roughly 150–250 MB of memory. If the Mac feels slow, Stop it and tell Claude.

## Kalshi API keys: not needed yet
Phases 1–6 only read public market data and paper trade. Keys are needed only for the demo
exchange (Phase 7). When we get there, you'll create the key on kalshi.com and save the file
into this folder yourself. Never paste keys into the chat.

## Everyday use
Look at the dashboard. The big word in the top left is the current decision; it says
**PASS** until all eight trade gates are green. Or ask Claude in the project: "is it running?",
"why did we pass?", "start paper trading".
