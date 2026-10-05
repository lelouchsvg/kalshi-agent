# Start here (zeke)

You don't need Python or anything technical on your Mac. Claude writes and tests the
code in the cloud, it goes to a private GitHub repo, and a small rented Linux server
runs the agent 24/7 and shows you the dashboard in your browser.

```
Your Mac (browser + Claude app)
   │  you talk to Claude, you look at the dashboard
   ▼
Claude's cloud workspace ──push──▶ GitHub (private repo)
                                       │ server pulls every 10 min, runs tests,
                                       ▼ keeps the update only if tests pass
                              VPS (~$5/mo Linux server)
                              ├─ collector (Kalshi + crypto data → database)
                              ├─ dashboard (https://<your-ip>.sslip.io, password protected)
                              └─ daily backups
                                       │
                                       ▼
                              Kalshi API (read-only market data in Phase 1)
```

## What only you can do (about 30 minutes, once)

### 1. Make a private GitHub repository (2 minutes)
- **Where:** github.com, signed in as lelouchsvg → the **+** at top right → **New repository**
- **Type:** name `kalshi-agent`, choose **Private**, leave every checkbox empty, click **Create repository**
- **Why:** this is where the code lives so the server can download it.
- **Done when:** you see an empty repo page. Tell Claude "repo created".

### 2. Rent the server (10 minutes, about $5/month)
Recommended: **Hetzner Cloud** (cheapest reliable option; pick a **US location** such as Ashburn,
because Kalshi serves US users). DigitalOcean or Vultr also work at a similar price.
- **Where:** hetzner.com/cloud → sign up → **Add Server**
- **Pick:** Location *Ashburn, VA* · Image *Ubuntu 24.04* · Type the smallest shared x86 plan (2 GB RAM is plenty) · Authentication **Password** is fine to start
- **Why:** the agent must keep running when your Mac is off.
- **Done when:** the server shows a green "Running" dot and an IPv4 address. Check the monthly price shown before you click Create.

### 3. Run the setup script (10 minutes)
- On the server's page click **Console** (a black terminal window opens in your browser; log in as `root` with the password Hetzner emailed or showed you).
- **Paste A** (creates the server's key and shows it):
  ```
  apt-get update -qq && apt-get install -y -qq git >/dev/null
  id kalshi >/dev/null 2>&1 || adduser --disabled-password --gecos "" kalshi
  sudo -u kalshi bash -c 'mkdir -p ~/.ssh && { [ -f ~/.ssh/deploy_key ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/deploy_key -q; }; printf "Host github.com\n IdentityFile ~/.ssh/deploy_key\n StrictHostKeyChecking accept-new\n" > ~/.ssh/config; cat ~/.ssh/deploy_key.pub'
  ```
- It prints a line starting with `ssh-ed25519`. That is a **public** key (safe to copy, not a secret). In GitHub: your repo → **Settings** → **Deploy keys** → **Add deploy key** → paste it, title "vps", leave *Allow write access* **off** → **Add key**.
- **Paste B** (downloads the code, tests it, starts everything):
  ```
  sudo -u kalshi git clone git@github.com:lelouchsvg/kalshi-agent.git /home/kalshi/kalshi-agent
  bash /home/kalshi/kalshi-agent/deploy/setup_vps.sh git@github.com:lelouchsvg/kalshi-agent.git
  ```
- **Done when:** it prints `Dashboard: https://….sslip.io` and a password. Save the password in your password manager. Never paste it into the chat.

### 4. Kalshi API keys: not needed yet
Phase 1–6 only read public market data and paper trade. Keys are needed only for the demo
exchange (Phase 7). When we get there, you'll create the key on kalshi.com and put the file
on the server yourself, never in the chat.

## Everyday use
Open the dashboard link. The big word in the top left is the current decision; it says
**PASS** until all eight trade gates are green. The red **KILL SWITCH** stops everything.

Or just ask Claude in this thread: "is it running?", "start paper trading", "why did we pass?".
