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

## Installing an update from Claude (keeps all collected data)
1. Download the new `kalshi-agent.zip` and double-click it. You get a `kalshi-agent` folder in Downloads.
2. Open Terminal, type `bash ` (with a space), drag `install_update.sh` from that **new** folder
   into the window, and press Enter.
3. It stops the agent, backs up your database to `data/backups/`, copies the new code into
   `~/kalshi-agent`, runs the safety tests and restarts. Your data, logs and `.env` are never touched.

Don't drag the new folder over the old one in Finder: Finder's "Replace" deletes the old folder,
including the collected data.

## Things to know about running on a laptop
- **It only works while the Mac is awake.** The agent keeps the Mac from idle-sleeping while
  it runs, but closing the lid still puts it to sleep. Keep it plugged in with the lid open.
- **Sleep and Wi-Fi drops are handled safely.** When data goes stale, the health check fails
  and the agent will not trade (later phases), then it resumes on its own.
- **Gaps in data are normal.** Hours the Mac was off are simply missing from the research
  data; the models will be trained only on what was actually recorded.
- It uses roughly 150–250 MB of memory. If the Mac feels slow, Stop it and tell Claude.

## Demo exchange (Phase 7, fake money)
With a key from a free practice account at demo.kalshi.co (saved in the dashboard's **Demo
exchange** box), each paper trade is also placed as a real order on Kalshi's demo exchange, at
most 2 contracts and only if the demo price is within 2¢ of the paper price. This tests order
sending, fills, fees and settlement end to end. It can never reach your real account.

## Updates install themselves
Once a read-only GitHub token is saved in the dashboard's **Automatic updates** box, the agent
checks GitHub every 30 minutes. It runs the safety tests on any new version and installs it
only if every test passes, keeping your data, `.env` and keys. If a new version won't start,
the previous one is put back automatically. An automatic update can never unlock real-money
trading. Those changes always need a manual install. You can still update by hand with
install_update.sh.

## Kalshi API key (optional)
The agent works without one. A key adds Kalshi's real-time stream. To add it, open the
dashboard, find **Kalshi account**, paste the Key ID and the private key from kalshi.com, and
press **Save key**. It is saved only on this Mac (`secrets/kalshi.key`, readable only by you,
plus two lines in `.env`) and the agent restarts its data collector by itself. A key never
unlocks real-money trading. Never paste keys into the chat.

## Everyday use
Look at the dashboard. The big word in the top left is the current decision; it says
**PASS** until all eight trade gates are green. Or ask Claude in the project: "is it running?",
"why did we pass?", "start paper trading".
