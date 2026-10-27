# EleSync — Setup Guide
### Your AI memories. On your computer. Forever yours.

---

EleSync takes the memories ChatGPT, Claude, and Gemini have about you and puts them
in one private folder on your own computer (or USB drive). No accounts. No cloud.
No subscriptions. You own your data completely.

**This guide works even if you've never used a terminal before.**
Every step is explained in plain English. Most people finish in under 10 minutes.

---

## Before you start — what you need

| What | Where to get it | Free? |
|------|-----------------|-------|
| A Windows, Mac, or Linux computer | — | — |
| Python 3.11 or newer | We walk you through it in **Step 0** below | ✅ |
| Your chat export files (see Step 2) | From each AI app | ✅ |

> **Do I need an AI client?** No — EleSync works completely standalone.
> Connecting Claude, Cursor, or ChatGPT is optional, for when you want your AI
> to automatically remember you. The core features (import, search, summary) work with no AI at all.

---

## How to open a terminal (the command box)

You'll type a few commands here. Don't worry — they're all copy-paste.

**Windows**
Press the **Windows key**, type **PowerShell**, click it.

**Mac**
Press **Cmd + Space**, type **Terminal**, press Enter.

**Linux**
Right-click the desktop → "Open Terminal", or press **Ctrl + Alt + T**.

A window with a blinking cursor appears. You're ready.

> 💡 **Pasting into the terminal:**
> Windows → right-click to paste.
> Mac / Linux → Cmd+V or Ctrl+Shift+V.

---

## PART 1 — Install on your computer

### Step 0 — Get Python (one time, about 3 minutes)

EleSync runs on something called **Python**. It's free, safe, and made by a
non-profit. You install it once and never think about it again.

**First, check if you already have it.** Open the terminal (see above) and type:

```
python --version
```

If you see `Python 3.11` or any higher number (like 3.11, 3.12, 3.13) — **you're
already done. Skip to Step 1.** If you see an error or a number lower than 3.11,
follow the steps for your computer below.

---

**Windows**

1. Go to **[python.org/downloads](https://www.python.org/downloads/)**
2. Click the big yellow **"Download Python"** button
3. Open the file you just downloaded
4. ⚠️ **VERY IMPORTANT:** at the bottom of the first screen, tick the box that
   says **"Add python.exe to PATH"** before clicking anything else. If you miss
   this, EleSync won't work — so look for it.
5. Click **"Install Now"** and wait for it to finish
6. Close and reopen the terminal, then type `python --version` again to confirm

---

**Mac**

1. Go to **[python.org/downloads](https://www.python.org/downloads/)**
2. Click the big yellow **"Download Python"** button
3. Open the downloaded file (ends in `.pkg`) and click **Continue → Continue →
   Agree → Install**. Enter your Mac password if asked.
4. When it finishes, open the terminal and type `python3 --version` to confirm

> 💡 On Mac, use **`python3`** (with the 3) anywhere this guide says `python`.

---

**Linux**

You almost certainly already have it. If `python3 --version` showed an error,
install it with:

```
sudo apt install python3 python3-pip
```

(On Fedora: `sudo dnf install python3 python3-pip`. Enter your password if asked.)

---

> ✅ **Once `python --version` shows 3.11 or higher, Python is set up forever.**
> You never have to do this again, on this computer.

---

### Step 1 — Install EleSync

EleSync is not on PyPI, so `pip install elesync` will
not find it. Install it from the GitHub repository.

> 💡 **On Windows and don't want the terminal?** Download `EleSync-Setup.exe` from the
> latest release instead (see the [README](README.md#install)) and use the dashboard.

1. Open
   [github.com/darknodebros/EleSync](https://github.com/darknodebros/EleSync).
2. Click the green **Code** button → **Download ZIP**. Leave the file zipped.
3. Paste this into the terminal (change the path to where the ZIP was saved) and press **Enter**:

```
pip install "C:\Users\YourName\Downloads\EleSync-master.zip"
```

A bunch of text scrolls by — that's normal. When it stops, check it worked:

```
ele --version
```

You should see **`EleSync`** followed by a version number. ✅

> ❓ **See "ele is not recognized"?**
> Paste this instead — it works the same way:
> `python -m elesync.cli --version`
> From now on, whenever this guide says `ele`, use `python -m elesync.cli`.

---

### Step 2 — Export your memories from your AI apps

EleSync reads files you export from each AI. It accepts JSON, CSV, TXT, Markdown, and ZIP archives. ZIP files are containers: EleSync safely imports only recognized files inside them and skips account, billing, media, and unrelated files.

---

**ChatGPT**

1. Go to [chat.openai.com](https://chat.openai.com) → click your profile picture (top right)
2. **Settings** → **Data controls** → **Export data** → click **Export**
3. Wait for an email from OpenAI (usually a few minutes)
4. Download the ZIP file in the email → open it
5. Find the file called **`memory.json`** — that's what you need

---

**Claude**

1. Go to [claude.ai](https://claude.ai) → click your name (bottom left)
2. **Settings** → **Privacy** → **Export data**
3. Download the ZIP → open it
4. Find the file ending in **`_memory.json`**

---

**Grok**

1. Request your data export from Grok/xAI account settings or X data settings
2. Download and extract the ZIP archive
3. Find `prod-grok-backend.json` — this is the conversation export EleSync can read
4. Keep the other account, billing, and media files private; EleSync only needs the conversation JSON

---

**Gemini**

1. Go to [takeout.google.com](https://takeout.google.com)
2. Click **Deselect all** → then tick only **Gemini**
3. Click **Next step** → **Create export**
4. Wait for the email → download the ZIP → open it
5. Find the `.json` file inside the Gemini folder

---

**WhatsApp**

1. Open the WhatsApp chat you want to keep
2. Choose **More → Export chat → Without media**
3. Save the `.txt` file somewhere easy, such as Downloads
4. EleSync will show a privacy review before saving anything

WhatsApp exports may contain private messages from other people. EleSync does not upload them. You can save the full conversation or approve selected messages, with options to redact phone numbers, remove links, remove media-only messages, and limit the import to one sender.

---

> 💡 **Save those files somewhere easy** — your Desktop or Downloads folder works great.
> You'll point EleSync at them in the next step.

---

### Step 3 — Import your memories

For each file you downloaded, run the matching command.
Replace the file path with the actual location of your file.

**ChatGPT memories:**
```
ele import "C:\Users\YourName\Downloads\memory.json" --source chatgpt
```

**Claude memories:**
```
ele import "C:\Users\YourName\Downloads\claude_memory.json" --source claude
```

**Gemini memories:**
```
ele import "C:\Users\YourName\Downloads\gemini_memory.json" --source gemini
```

**Grok conversations:**
```
ele import "C:\Users\YourName\Downloads\prod-grok-backend.json" --source grok
```

EleSync reads the extracted `conversations` and `responses` structure locally. It ignores unrelated export files and does not need Grok account, billing, or media files.

**WhatsApp conversations:**

Use the dashboard Import page for the privacy review:

```text
1. Run: ele web
2. Open Import in the dashboard
3. Drop in your WhatsApp .txt export
4. Review and approve the full chat or selected messages
```

The CLI can also import a WhatsApp export directly when you have already reviewed the file:

```
ele import "C:\Users\YourName\Downloads\WhatsApp Chat.txt" --source whatsapp
```

> 💡 **Easy file path trick:** type `ele import ` (with a space), then
> **drag the file** from your file explorer directly into the terminal window.
> The path fills in automatically!

You'll see something like:
```
Imported 47 new memories (3 duplicates skipped). Vault now holds 47.
```

**You can run this as many times as you like** — EleSync never creates duplicates.
Re-importing the same file is always safe.

---

### Step 4 — See what's in your vault

Check everything imported correctly:

```
ele summary
```

You'll see all your memories grouped by type — facts, preferences, projects, notes —
with which AI they came from and when. For a quick overview:

```
ele summary --short
```

To search for something specific:

```
ele search "project"
ele search "preferences"
```

**You're done with the basics.** Your memories are saved locally on your computer
in a folder called `EleSyncVault` in your home directory. Plain text files.
Nothing leaves your machine.

---

### Step 4½ (optional) — Prefer clicking to typing? Open the web UI

If you'd rather see and manage your memories in a friendly window instead of
the terminal, run:

```
ele web
```

Your browser opens automatically at **http://127.0.0.1:7477** with a clean
dashboard where you can:

- **Memories** with one search space for conversations and saved memories
- **Conversations** grouped by date with expandable conversation cards
- **Projects** that combine content from ChatGPT, Claude, Gemini, WhatsApp, and other sources
- **Save** a new memory with a simple form (or press `N`)
- **Import** JSON, CSV, TXT, Markdown, ZIP archives, AI exports, or WhatsApp chats with privacy review
- **Edit or delete** saved content by clicking it
- **Export** your vault as JSON or CSV
- **Install** the dashboard as a phone PWA from the Use on another device page
- **Toggle dark mode** with one click (your preference is remembered)

**Keyboard shortcuts:** `/` focuses Memories, `N` saves something, `?` shows help, `Esc` closes, `Ctrl+K` opens the command menu.

It runs entirely on your own computer (`127.0.0.1` means "this machine only" —
nothing is exposed to the internet). Press **Ctrl + C** in the terminal to stop it.

> 💡 If port 7477 is busy, pick another: `ele web --port 8080`
> Don't want it to open a browser automatically? `ele web --no-browser`

> 🔒 **Using an encrypted vault?** `ele web` will ask for your passphrase before
> it starts, then decrypts on the fly — your memories are never written unencrypted.

> 📱 **Want to install EleSync on your phone?**
> Open **Use on another device** in the dashboard after adding a user with `ele web adduser yourname`. Use its HTTPS phone-pairing tunnel, or supply `--tls-cert` and `--tls-key` when starting `ele web --host 0.0.0.0`. Direct network access requires TLS by default. A TLS-terminating reverse proxy should forward to a loopback listener. Open the HTTPS address on your phone and choose **Add to Home Screen**. The computer must remain running for live access.

---

### Phone PWA notes

- The PWA is installed from the browser; there is no separate mobile download.
- The computer hosting EleSync must be running for live data.
- If you save a memory while the phone is temporarily disconnected, EleSync keeps an encrypted offline draft in that browser and retries it when the connection returns.
- Use the command menu (`Ctrl+K`) and choose **Clear offline drafts** if you want to remove drafts from that phone.
- Shared text, links, and titles open in EleSync's Save Something screen for review before saving.
- Do not use an unprotected public URL. Use a dashboard passcode and HTTPS for access outside your private network.

---

### Step 4¾ (optional) — Password-protect the web UI

If you're on a shared computer or exposing the dashboard to your local network,
add a login:

```
ele web adduser yourname
```

You'll be asked to set a password. From now on, the web UI requires login.

Want a read-only account for someone who should browse but not edit?

```
ele web adduser viewer_name --role viewer
```

To manage accounts:

```
ele web listusers
ele web removeuser viewer_name
```

Remove the last user and login turns back off — back to open access on localhost.

---

### Step 5 (optional) — Connect to your AI client

If you use Claude Desktop, Cursor, Windsurf, or any other MCP-capable client and want it to automatically know your memories:

```
ele onboard
```

Then **restart your AI client** so it picks up the new MCP server.

If you want to connect ChatGPT, Grok, or Gemini through a custom connector, use the connector helper instead:

```
ele connect chatgpt --allowed-host your-tunnel-or-domain.example
ele connect grok --allowed-host your-tunnel-or-domain.example
ele connect gemini --allowed-host your-tunnel-or-domain.example
```

EleSync will print:

- the server URL to paste into the AI client;
- the Bearer token the AI client must use;
- the `ele serve --transport http ...` command to keep running on your computer.

Remote/custom connectors need a reachable HTTPS address, so use a tunnel or reverse proxy that points back to the local EleSync server.

For Cursor or Claude Desktop:

```
ele connect cursor
ele connect claude
```

Those commands print the local setup command and restart step.

Ask it: *"What do you remember about me?"* — it'll know. 🎉

---

---

## PART 2 — Install on a USB drive (take your memories anywhere)

Want your memories on a USB stick so you can plug it into any computer and
Claude already knows you — with zero setup on that machine? This is for you.

### What you need

- A USB drive (any size — even a 1 GB drive is more than enough)
- EleSync installed (encryption is included in the base install)

---

### USB Step 1 — Plug in your USB drive

Plug in the drive. Note its path:

| System | What to look for | Example |
|--------|-----------------|---------|
| **Windows** | The letter shown in File Explorer | `E:\` or `F:\` |
| **Mac** | The name under /Volumes | `/Volumes/MyUSB` |
| **Linux** | Usually under /media or /mnt | `/media/yourname/MyUSB` |

Not sure? Run this to see all connected drives:

```
ele usb list
```

---

### USB Step 2 — Set up the encrypted vault on the drive

Run this (replace the path with your actual drive path):

```
ele usb install /Volumes/MyUSB
```

```
ele usb install E:\
```

You'll be asked to **choose a passphrase**. This encrypts everything on the drive —
**if the drive is ever lost or stolen, nobody can read your memories without this passphrase.**

> ⚠️ **Don't forget this passphrase.** There is no recovery — that's what makes it secure.
> Write it down somewhere safe if you need to.

When it finishes you'll see:

```
✅  Vault created:   /Volumes/MyUSB/EleSyncVault
   Encrypted:      yes (XSalsa20-Poly1305 + argon2id)
   Launcher:       /Volumes/MyUSB/RUN_ME.py
   README:         /Volumes/MyUSB/ELESYNC_README.txt
```

Two files are now on your drive:
- **`RUN_ME.py`** — run this on any new machine (see USB Step 4)
- **`ELESYNC_README.txt`** — plain-English reminder of all commands

---

### USB Step 3 — Import your memories onto the drive

Same as Part 1 Step 3, but with `--vault` pointing at the USB:

```
ele --vault /Volumes/MyUSB/EleSyncVault import "~/Downloads/memory.json" --source chatgpt
ele --vault /Volumes/MyUSB/EleSyncVault import "~/Downloads/claude_memory.json" --source claude
ele --vault /Volumes/MyUSB/EleSyncVault import "~/Downloads/gemini_memory.json" --source gemini
```

Check what's there:

```
ele --vault /Volumes/MyUSB/EleSyncVault summary
```

---

### USB Step 4 — Connect to your AI client on this machine

```
ele usb attach /Volumes/MyUSB
```

Enter your passphrase. Then restart your AI client. Done.

---

### USB Step 5 — Before unplugging

**Always run this before removing the drive** — it disconnects Claude
so it doesn't show an error when the drive is gone:

```
ele usb detach /Volumes/MyUSB
```

Or detach and eject in one go:

```
ele usb detach /Volumes/MyUSB --eject
```

---

### USB Step 6 — Using the drive on a different computer

This is the magic part. On any computer with Python and EleSync installed
(Part 1, Steps 0–1):

1. Plug in the drive
2. Open a terminal
3. Run:

```
python /Volumes/MyUSB/RUN_ME.py
```

(Replace the path with your drive's actual path — on Windows: `python E:\RUN_ME.py`)

That script does everything automatically:
- Connects the USB vault to your AI client
- Shows you what to do next

**That's it.** Plug in. Run one command. Claude knows you.

---

---

## Quick reference — all the commands

### Everyday commands

| What you want to do | Command |
|---------------------|---------|
| See all your memories | `ele summary` |
| Quick overview | `ele summary --short` |
| Search for something | `ele search "keyword"` |
| Add a memory manually | `ele add "I prefer dark mode" --type preference` |
| Import a new export file | `ele import ~/Downloads/memory.json --source chatgpt` |
| Delete a memory | `ele forget <id shown by search>` |
| Check everything is working | `ele doctor` |

### USB commands

| What you want to do | Command |
|---------------------|---------|
| See connected drives | `ele usb list` |
| Set up a USB vault | `ele usb install /path/to/drive` |
| Connect USB to Claude | `ele usb attach /path/to/drive` |
| Disconnect before unplugging | `ele usb detach /path/to/drive` |
| Check USB vault status | `ele usb status /path/to/drive` |
| Safe-eject | `ele usb eject /path/to/drive` |
| Set up on a new machine | `python /path/to/drive/RUN_ME.py` |

---

## Something's not working?

**"ele is not recognized" / "command not found"**
→ Use `python -m elesync.cli` instead of `ele` for every command.

**"pip is not recognized" / "python is not recognized" (Windows)**
→ This almost always means the **"Add python.exe to PATH"** box wasn't ticked when
Python was installed (Step 0). Easiest fix: re-run the Python installer, choose
**Modify**, and make sure that box is ticked. Or, as a quick workaround, use
`py -m pip ...` instead of `pip ...` · On Mac/Linux use `python3 -m pip ...`

**Claude says it doesn't know anything**
→ You probably just closed the window. **Fully quit Claude** (Cmd+Q on Mac,
or tray icon → Quit on Windows) then reopen it.

**Wrong passphrase / can't unlock USB vault**
→ Make sure you're typing the exact passphrase you set during `ele usb install`.
There is no reset — that's what keeps it secure.

**Not sure what's wrong**
→ Run `ele doctor` — it checks everything and tells you in plain words what to fix.

**Still stuck?**
→ Open an issue at [github.com/darknodebros/EleSync/issues](https://github.com/darknodebros/EleSync/issues)
and paste the output of `ele doctor`. We'll help you out.

---

## Your data — what's where

```
~/EleSyncVault/            ← your local vault (home computer)
  notes/                   ← one .md file per memory — plain text, readable in any editor
  index.db                 ← search index (auto-rebuilt, safe to delete)
  vault.json               ← only present if encrypted

/Volumes/MyUSB/            ← your USB drive
  EleSyncVault/            ← same layout as above, always encrypted
  RUN_ME.py                ← one-command setup for new machines
  ELESYNC_README.txt       ← plain-English reference
```

Everything is plain Markdown files. You can open, read, and edit them in any text editor.
Nothing is ever sent anywhere — your memories stay exactly where you put them.

---

*EleSync is proprietary software — see [LICENSE](LICENSE).*
