# Pilot setup

## 1. Install

Livery is one file, `Livery-0.1.0-x86_64.AppImage`, for 64-bit Linux with glibc 2.35 or
newer (Ubuntu 22.04+, Debian 12+, Fedora 38+, Arch).

```
chmod +x Livery-0.1.0-x86_64.AppImage
./Livery-0.1.0-x86_64.AppImage
```

If it does not start and mentions FUSE, install `libfuse2` (Ubuntu: `sudo apt install libfuse2t64`),
or run it without FUSE: `./Livery-0.1.0-x86_64.AppImage --appimage-extract-and-run`.

Everything that converts and checks documents is inside the AppImage (Pandoc 3.11, Typst
0.15.1, Poppler, the Livery engine, the fonts). Projects are stored in
`~/.local/share/livery/` (set `LIVERY_HOME` to move them), never inside the AppImage.

## 2. Bubblewrap (required)

Livery renders every design an assistant touched, and runs every assistant, inside a
Bubblewrap sandbox. Without it, nothing renders and the assistants stay disabled: the app
never runs them unrestricted.

| Distribution | Command |
|---|---|
| Ubuntu, Debian | `sudo apt install bubblewrap` |
| Fedora | `sudo dnf install bubblewrap` |
| Arch | `sudo pacman -S bubblewrap` |

Unprivileged user namespaces must be allowed (the default almost everywhere). On Ubuntu
24.04, if **Set up** reports "Bubblewrap could not create a sandbox", AppArmor restricts them;
allow them for bwrap with an AppArmor profile, or as a quick test:
`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`.

## 3. Optional: LibreOffice

With `soffice` installed, the **Original** view shows each Word file as Word lays it out, next
to the restyled version. Without it everything else works.

## 4. Assistants

Install the assistant(s) you want to use, the official way. Livery uses the installed
command and does not install or update agents.

- **Claude Code**: docs.claude.com → Claude Code → Set up. Livery needs the native
  `claude` executable (the default installer provides it).
- **Codex**: developers.openai.com/codex. The `codex` executable from the official
  installer or npm package.

Livery signs in with **its own profile** for each assistant, separate from the profile you
use in a terminal, so your usual settings, plugins, hooks and MCP servers are never loaded.
Open **Set up** in the app; for each assistant it shows the exact commands with the profile
folder already filled in:

| | Subscription | API key |
|---|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR=…/profiles/claude claude auth login` (or `claude setup-token`, then paste the token) | paste an Anthropic API key |
| Codex | `CODEX_HOME=…/profiles/codex codex login` (or `codex login --device-auth`) | paste an OpenAI API key |

"Open in terminal" runs the command in your terminal emulator; finish the sign-in there and
press **Refresh**. Keys and tokens are stored in the profile folder with owner-only
permissions.

## 5. Check

**Set up** lists the assistants (installed, version, signed in), the Bubblewrap status and the
bundled tool versions. A green dot next to an assistant in the chat means it is installed,
signed in and isolated.

## Troubleshooting

- **"Isolation unavailable"**: see step 2. The message under **Set up** is Bubblewrap's own.
- **An assistant says it is not logged in**: sign in again from **Set up**; the error in the
  chat has a **Sign in…** button.
- **A document shows FAIL**: open it and expand **Verification** under the preview. Each check
  says what differs. Typical causes: unresolved tracked changes (accept or reject them in Word,
  then **Replace** the document), or very long words overflowing a narrow table column (give
  the table a smaller font or different column proportions).
- **Logs**: start the AppImage from a terminal; the engine writes warnings to stderr.
