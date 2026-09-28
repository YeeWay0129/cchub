# cchub

**English** · [繁體中文](README.zh-TW.md)

Use Claude Code on your Linux machine from your phone: create a new project folder, open any project, and start sessions in it. You do all of this from the official Claude mobile app, and never have to remote-desktop into the computer.

cchub is a thin helper around Claude Code's official **Remote Control** server mode (`claude remote-control`). It adds no protocol, no app and no chat UI of its own. Everything you see on the phone is the official Claude app. cchub adds only the two things Remote Control can't do by itself:

1. **Start and keep a Remote Control server in a given folder.** It runs under systemd, restarts after crashes and network outages, and picks up Claude Code updates.
2. **Create a new project folder from the phone.** The folder gets a `CLAUDE.md` containing your request, `git init`, and workspace trust. You confirm this once on the phone.

> **Unofficial.** cchub is not affiliated with or endorsed by Anthropic. It relies on how Claude Code 2.1.28x behaved when it was written (September 2026), so a future Claude Code update can break it. The CLI messages and the bundled skill are in Traditional Chinese.

## Why

Remote Control lets you continue a session on your computer from your phone, with these limits:

- Someone has to **start** the server on the computer first. You can't start a session on your machine from the phone unless a server is already running in that folder.
- The phone's device picker shows **one entry per running server**. It doesn't let you browse into subfolders, so each project needs its own server.
- A new folder needs **workspace trust** before a server will start in it. A git repository is its own trust boundary, so a trusted parent folder doesn't cover it.
- A server **exits after about 10 minutes offline**. Something has to bring it back.

cchub fills these gaps.

## How it works

```
Phone: Claude app → Code → your computer
 ├─ "projects" = the entry server (your projects root; always on, starts at boot)
 │    └─ you: "create a new project ledger: a small expense tracker"
 │         └─ Claude (via the cchub skill) runs `cchub new ledger --brief-stdin`
 │              ├─ the phone asks you to confirm once
 │              ├─ creates <projects-root>/ledger (CLAUDE.md with your request, git init)
 │              ├─ trusts only this freshly created folder
 │              └─ systemctl --user start cchub-rc@<ledger>
 └─ "ledger" = a project server
      └─ Directory: ledger → "+ New session" → "go ahead with the request in CLAUDE.md"

cchub-reconcile.timer (every 5 min): swaps in new Claude Code builds,
records permanent errors, and makes sure the entry server is running
```

## Requirements

| Requirement | How to check |
|---|---|
| Linux with systemd user services, and lingering enabled so the entry starts at boot without a login | `loginctl show-user $USER -p Linger` |
| Python 3 (standard library only; tested on 3.12) and git | `python3 --version` |
| Claude Code CLI **2.1.281 or newer**, logged in with a claude.ai subscription. API keys and `claude setup-token` tokens can't use Remote Control. | `claude auth status` |
| The one-time Remote Control consent is answered: run `claude remote-control` once in a terminal and answer `y` | `cchub doctor` |
| Your projects root is trusted: run `claude` there once and accept the trust dialog | `cchub doctor` |
| `~/.local/bin` is on your `PATH` | `echo $PATH` |
| A secured claude.ai account (strong password and 2FA) and a locked phone | See [Security](#security) |

## Install

```bash
git clone https://github.com/YeeWay0129/cchub.git
cd cchub

# 1. See exactly what will be done. This changes nothing.
bin/cchub install --dry-run --projects-root ~/code

# 2. Install. Run it in your own terminal; it asks you to type "yes".
bin/cchub install --projects-root ~/code
```

`--projects-root` is required on the first install. It is where new projects are created and where the entry server runs. `--allowed-root DIR` (repeatable) adds more folders you can open. `--entry-dir DIR` puts the entry somewhere else.

A real install or uninstall is refused inside a Claude Code session and without a TTY. `--dry-run` always works.

What `install` does (all of it can be reverted with `cchub uninstall`):

1. Copies `bin/`, `cchub/` and `templates/` to `~/.local/share/cchub/`, so the entry session can't edit its own tool.
2. Links `~/.local/bin/cchub` to that copy.
3. Writes `~/.config/systemd/user/cchub-rc@.service`, `cchub-reconcile.service` and `cchub-reconcile.timer`, then runs `daemon-reload`.
4. Writes the skill `~/.claude/skills/cchub/SKILL.md`. It first backs up any existing file with different content.
5. Adds six `permissions.ask` rules to `~/.claude/settings.json` (listed below). It backs up the file first and changes nothing else.
6. Writes `~/.config/cchub/config.json` (only if it doesn't exist), then enables and starts the entry server and the reconcile timer.
7. Runs `cchub doctor`.

## Using it from the phone

In the Claude app, go to **Code** → your computer. The sheet shows a **Directory** picker, the active sessions, and a **+ New session** button.

- **New project:** Choose the entry directory (your projects root) and start or open a session. Say something like *"create a new project ledger: a small expense tracker"*. Approve the one confirmation. When it replies "✅ ledger is up", choose **ledger** under Directory, tap **+ New session**, and say *"do the request in CLAUDE.md"*.
- **Open an existing project:** In the entry session, say *"open my-app"*. Then choose it under Directory and start a new session.
- **Another session in the same project:** Choose it under Directory and tap **+ New session**.
- **See results:** Ask Claude to publish the result as an Artifact, then tap the link.
- **Check or stop servers:** Ask *"what's running?"* (`cchub ls`) or *"stop ledger"* (asks you to confirm).

The reply to `new` or `open` ends with the server's **environment URL** (`https://claude.ai/code?environment=…`) as a fallback. cchub doesn't report session URLs: in Claude Code's output they only appear inside session-title lines, which can contain conversation text, so those lines are dropped.

## Commands

| Command | What it does | Confirmation |
|---|---|---|
| `cchub ls [--json]` | Lists managed servers: folder, state, uptime, warnings | – |
| `cchub open <name or path> [--mode M]` | Starts a server for an existing, already-trusted folder | – |
| `cchub logs <name> [-n N]` | Shows the filtered status log, which never contains conversation text | – |
| `cchub doctor` | Checks prerequisites and protections, and lists untrusted projects | – |
| `cchub new <name> [--title T] [--brief-stdin] [--mode M] [--no-git]` | Creates a project (CLAUDE.md, git, trust) and starts its server | **yes** |
| `cchub stop <name>` / `cchub restart <name>` | Stops or restarts a server. The entry can't be stopped. | **yes** |
| `cchub install` / `cchub uninstall` | Installs or removes cchub; both accept `--dry-run` | **yes** |

Rules worth knowing:

- **Names:** a name is a folder under an allowed root, or an absolute path. If two folders share a name, you're asked to pick the full path.
- **New projects:** names must match `^[a-z0-9][a-z0-9-]{0,39}$`. New projects are only created under the projects root.
- **Permission modes:** only `default`, `acceptEdits`, `auto`, `plan` and `dontAsk` are accepted. **`bypassPermissions` is always refused.** The default is `auto`. If your plan doesn't offer auto mode, change `default_mode` and `entry_mode` in `~/.config/cchub/config.json`.
- **Limit:** at most 6 project servers run at once (the entry doesn't count).
- **Your request text** is passed only through a quoted heredoc on stdin, so `$`, backticks and quotes in it are never expanded by the shell.

## Security

Read this before installing.

- **What cchub can do:**
  - Start a server for a folder that Claude Code already trusts.
  - Create an empty project and trust *that folder only*.
  - Stop a server.
- **What cchub can't do:** it has **no way to trust an existing folder**. Trust is written only for the folder that the same `cchub new` call just created, after it checks that the folder contains nothing but the template. If an existing folder isn't trusted, trust it yourself with Claude Code's own dialog: run `claude` in that folder.
- **The confirmations are a UX layer, not a security boundary.** Install adds these user-level ask rules:

  ```
  Bash(cchub new *)
  Bash(cchub stop *)
  Bash(cchub restart *)
  Bash(cchub install*)
  Bash(cchub uninstall*)
  Bash(cchub _*)
  ```

  They make the phone ask for confirmation, even in auto mode. But they only match the literal command, so calling cchub through an absolute path or `bash -c` skips them. That's why the security rests on what cchub is *able* to do, not on these prompts.
- **Exposure grows.** The entry server is always on and runs in `auto` mode by default. Anyone who gets into your claude.ai account, or picks up your unlocked phone, can run code on your computer at any time, not just while you have a session open. Use a strong password, 2FA, and a phone lock. Consider a stricter `entry_mode`.
- **The entry session is not a sandbox.** It inherits your user-level Claude Code settings and allow rules. In auto mode it can edit any project under your projects root, subject to Claude Code's own classifier.
- **Logs don't keep conversations.** cchub runs `claude remote-control` without `--verbose` and keeps only a whitelist of lines: connection status, the environment URL, and a few exact error messages. Error classification reads only stderr. Session titles and tool activity come from conversation text, can imitate anything, and are dropped.

## When you still need the computer

| Situation | What cchub does | What you do |
|---|---|---|
| An existing folder isn't trusted | `open` refuses | Run `claude` there once and accept the trust dialog |
| A folder inherits trust from its parent but has its own hooks, MCP servers or permissions | `open` refuses | Same as above, so you can review that configuration |
| The Claude Code login expired or you're logged out | The server stops and `cchub ls` shows why | Run `claude auth login` |
| Remote Control consent not answered | Keeps retrying; `cchub ls` shows why | Run `claude remote-control` once and answer `y` |
| The computer is off | Nothing can be done | Turn it on |

Network outages, sleep, and Claude Code updates that delete the running build all recover on their own.

## Uninstall

```bash
cchub uninstall --dry-run   # see what will be removed
cchub uninstall             # run in your own terminal
```

Uninstall does the following:

- Disables and removes the units and the timer, the skill, `~/.local/bin/cchub` and `~/.local/share/cchub`. Files that were changed after install are kept, with a warning.
- Reverts the trust that cchub added.
- Removes only the ask rules that cchub added, backing up `settings.json` first.

**Project folders are never deleted.**

## Development

```bash
python3 -m unittest discover -s tests
```

The tests use temporary directories and fake systemd/proc functions. They never touch your real `~/.claude.json` or systemd.

## License

[MIT](LICENSE)
