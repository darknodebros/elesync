# EleSync

## Your private AI workspace

EleSync gives you a private workspace that your AI tools can share.

When every AI assistant keeps a separate history, you repeat the same background,
preferences, decisions, and project details over and over. EleSync keeps the durable
context you choose in a local vault, then lets approved assistants use that context
with explicit project and scope boundaries.

Start a project with one assistant, continue it with another, and keep the context in
a folder you control.

```text
Your local vault
      ↓
Memories and imported history
      ↓
Projects, knowledge, tasks, decisions, and handoffs
      ↓
Approved AI assistants
```

No EleSync account, hosted cloud database, e-mail registration, telemetry, subscription,
or mandatory cloud sync is required.

## The five-minute path

A new user should be able to go from installation to a useful AI workspace like this:

1. **Choose a vault.** Pick the local folder where EleSync will keep your workspace.
2. **Protect access.** Add a local dashboard username and password when you need remote
   or shared-device access.
3. **Import AI history.** Bring in ChatGPT, Claude, Gemini, WhatsApp, or other supported
   exports and review what will become durable context.
4. **Create a project.** Give your memories a purpose and a boundary.
5. **Connect an assistant.** Connect Claude Desktop locally, or use a secure HTTPS/OAuth
   flow for a remote assistant such as ChatGPT, Gemini, or Grok.
6. **Test recall.** Ask your assistant: `What useful context can you retrieve from EleSync?`

The dashboard includes a guided first-launch wizard and progress tracking for these steps.

## What EleSync does

- **Memory:** gives assistants continuity across conversations and providers.
- **Projects:** give context purpose, organization, and boundaries.
- **Knowledge:** stores reusable knowledge sheets, tasks, decisions, and handoffs.
- **MCP/connectors:** let approved AI assistants participate in the workspace.
- **Imports:** bring existing AI history, documents, and WhatsApp conversations into one place.
- **PWA/mobile:** capture and access the workspace from a phone while your computer runs EleSync.
- **Privacy:** keeps the vault local-first and makes sharing explicit.

Important: when you connect an AI assistant, that assistant may read the context you make
available to it. Use project boundaries, scopes, read-only permissions, and separate vaults
when you need stronger limits.

## Visual tour

The dashboard is the center of the workspace: memories, projects, imports, connection health,
and mobile access are all reached from one place.

The product workflow is:

| Area | What you use it for |
| --- | --- |
| **Import** | Upload ChatGPT, Claude, Gemini, WhatsApp, JSON, CSV, Markdown, TXT, or ZIP exports. |
| **Memories** | Search, review, edit, and organize imported and saved context. |
| **Projects** | Collect memories, knowledge sheets, tasks, decisions, and handoffs. |
| **Connect AI** | Configure local Claude Desktop or secure remote AI connections. |
| **Connection Health** | See whether the vault, dashboard security, local clients, and remote setup are ready. |
| **PWA/mobile** | Capture text, links, and imports from a phone. |

## Privacy by design

- **Local-first:** notes and the search index live in the vault on your computer.
- **User-controlled:** you choose the vault folder, backups, and what becomes durable.
- **No mandatory cloud account:** normal local use works without an EleSync cloud service.
- **Optional encryption:** encrypted vaults use Argon2id and XSalsa20-Poly1305 at rest;
  plaintext remains the default for portability.
- **Scoped AI access:** client grants can limit which memory scopes an assistant can read or write.
- **Project isolation:** project-bound connections cannot recall or modify memories outside their project.
- **Reviewable imports:** imported conversations and WhatsApp messages can be reviewed before saving.

## Install

Install EleSync from the source repository. Public package registry distribution is
not currently available.

### Windows: easiest path

When a Windows installer is available, download `EleSync-Setup.exe` from the
[releases page](https://github.com/darknodebros/EleSync/releases), run it, and open EleSync.

The installer is not code-signed yet, so Windows SmartScreen may say "Windows protected
your PC". Choose **More info**, then **Run anyway**.

The installer asks where to keep the vault and visibly lists the AI assistants available
from **Connect AI** after installation:

```text
ChatGPT · Claude · Gemini · Grok · Perplexity · Copilot · DeepSeek
```

Claude Desktop is the optional local connection that can be pre-wired during installation.
Remote assistants require your authorization and a secure HTTPS connection, so the installer
does not silently configure them.

### Standalone builds

Standalone Windows builds (the installer plus portable zips of the app and the `ele` command)
are attached to each release in the
[repository](https://github.com/darknodebros/EleSync/releases), when available.

### From source (Python)

For macOS, Linux, or users who prefer Python tooling, install from a clone of the
repository:

```bash
git clone https://github.com/darknodebros/EleSync.git
cd EleSync
pipx install .
ele web
```

Requires Python 3.11 or newer. You can also use `pip install .` inside the clone.

## First import

From the dashboard:

1. Open **Import**.
2. Choose a supported file or drop it into the import area.
3. Review the detected format and privacy information.
4. For WhatsApp, filter by sender and approve only the messages you want to keep.
5. Save the full conversation or selected durable memories.
6. Open **Memories** to search what was imported.

Supported formats include:

```text
JSON · CSV · TXT · Markdown · ZIP · WhatsApp exports · Grok exports
```

Imports are local. Nothing becomes durable project knowledge until the user saves or approves it.

## First project

Open **Projects**, create a project, and give it a short purpose such as:

```text
Japan trip planning
Product launch
Biology assignment
EleSync workspace
```

Inside a project you can:

- Add existing memories
- Save knowledge sheets
- Track tasks
- Record decisions
- Leave AI handoffs
- Set AI read/write/conversation permissions
- Generate a project-specific local Claude connection

## First AI connection

### Claude Desktop: local project connection

From a project page, choose the Claude provider and select **Generate project connection**.
EleSync creates a command like:

```powershell
ele --vault "D:\notes" onboard --target claude-desktop --project PROJECT_ID --name elesync-project-a1b2c3d4
```

Run the command, restart Claude Desktop, and use the entry named:

```text
elesync-project-a1b2c3d4
```

This entry is project-bound. Memories saved through it are attached to the selected project.
It can coexist with a general `elesync` entry; remove or disable the general entry when you
need strict project-only visibility.

### ChatGPT, Gemini, Grok, and other remote assistants

Remote assistants require a public HTTPS origin and provider authorization. Use the **Connect AI**
page to generate the current setup instructions. EleSync will show:

- MCP server URL
- Secure authentication details
- Local server command
- Tunnel/reverse-proxy target
- Connection verification result

Do not use `127.0.0.1`, a private LAN address, or the dashboard port as a public provider URL.

## Mobile companion (PWA)

EleSync can be installed on a phone without a separate mobile app:

1. Start EleSync on your computer with `ele web`.
2. Open **Use on another device** in the dashboard.
3. Put your phone and computer on the same Wi-Fi network.
4. Open the displayed address on your phone.
5. Choose **Add to Home Screen** or **Install app**.

The computer must be running for live vault access. The PWA supports shared text and link capture,
encrypted offline drafts, automatic retry, and mobile imports. Do not expose the dashboard publicly
without HTTPS and a passcode.

## Common commands

```bash
ele web                         # open the local dashboard
ele doctor                      # check installation health
ele import memories.json        # import exported memories or notes
echo "I prefer dark mode" | ele import -
ele remember "Prefer short answers with examples"
ele recall "how should my AI answer me?"
ele sync                        # rebuild the local search index
ele summary                     # show a vault summary
```

Advanced commands cover encryption, backups, USB vaults, scopes, conflict resolution, HTTP serving,
and client wiring. See [SETUP.md](SETUP.md) for the full walkthrough.

## Troubleshooting

Run:

```bash
ele doctor
```

Common fixes:

- If the dashboard does not open, run `ele web` and copy the error.
- If an AI assistant cannot see memories, restart it after setup.
- If search looks stale, run `ele sync`.
- If an import looks wrong, keep the original export and check its source format.
- If a remote connector is rejected, verify HTTPS, the public `/mcp` URL, the tunnel target,
  and the authentication state in Connection Health.

## Documentation

- [Setup guide](SETUP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security policy](SECURITY.md)
- [Release process](RELEASING.md)
- [Changelog](CHANGELOG.md)

## Project status

EleSync is at **1.24.78** (1167 tests, ~12,500 LOC). The current product focus is stabilization:
bug fixing, UX polish, documentation, first-run completion, privacy clarity, and reliability.

## License

EleSync is proprietary software.

Historical versions of EleSync that were previously released under the MIT License remain governed by the licenses attached to those specific releases. Current proprietary development does not revoke or alter rights already granted under those historical MIT releases.
