# tuwel-mcp

An MCP server that connects AI agents (Claude Code, Claude Desktop, any MCP client) to
**TUWEL**, the Moodle LMS of TU Wien (<https://tuwel.tuwien.ac.at>). Ask your agent about
courses, materials, deadlines, grades and announcements, and let it read lecture PDFs directly.

## How authentication works

TUWEL logs in through the TU Wien SSO (SAML, `idp.zid.tuwien.ac.at`), which asks for
username, password **and** a 6-digit TOTP code in a single form. This server does the same
login the official Moodle mobile app does, but over plain HTTP with no browser:

1. `admin/tool/mobile/launch.php` → Moodle login page → TU Wien IdP form
2. Username + password + TOTP are posted; the SAML assertion is relayed back to Moodle
3. Moodle redirects to `moodlemobile://token=…`. The server captures and verifies that
   **web-service token** (its signature is checked against the request's random passport)

The token is long-lived (usually weeks to months), so 2FA is only needed occasionally.
When the token expires, there are three ways to renew it:

| Mode | What is stored | Renewal |
|---|---|---|
| **Code on demand** (default) | username + password in the OS credential store (Windows Credential Manager / macOS Keychain) | The agent asks you for the current 6-digit code and calls `tuwel_login` |
| **Fully automatic** | additionally your TOTP secret | Silent re-login, no prompts. Convenient, but the secret on disk turns 2FA into a single factor on this machine. |
| **Token only** | nothing | Run `tuwel-mcp login` again yourself |

The token is stored in `%APPDATA%\tuwel-mcp\session.json` (Linux/macOS: `~/.config/tuwel-mcp/`).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> tuwel-mcp
cd tuwel-mcp
uv sync
uv run tuwel-mcp login
```

`login` prompts for your username, password and the current 2FA code (or your TOTP secret),
then offers to store the credentials. Check the result with:

```bash
uv run tuwel-mcp status
```

Other commands: `tuwel-mcp logout [--forget]`, and `tuwel-mcp login --browser`, a manual
fallback (e.g. for ID Austria) where you paste the `moodlemobile://token=…` link yourself.

Replace `/path/to/tuwel-mcp` below with the folder you cloned into (in JSON on Windows,
escape backslashes: `C:\\path\\to\\tuwel-mcp`).

### Register with Claude Code

```bash
claude mcp add tuwel --scope user -- uv run --directory /path/to/tuwel-mcp tuwel-mcp
```

### Claude Desktop / other MCP clients

```json
{
  "mcpServers": {
    "tuwel": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/tuwel-mcp", "tuwel-mcp"]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `tuwel_status`, `tuwel_login(totp_code)` | Connection check / token renewal |
| `list_courses(status)` | Enrolled courses (`inprogress`, `past`, `future`, `all`) |
| `get_course_contents(course_id)` | Sections, activities (with `cmid`), files and links |
| `read_module(cmid)` | Read any activity: page text, PDF/slides, assignment, quiz, folder, forum, URL |
| `read_file(url, offset)` | Download a file and return its text (PDF, DOCX, PPTX, code, HTML), paged |
| `download_course_files(course_id)` | Mirror all course files locally |
| `search_courses_content(query)` | Search names, descriptions and filenames across courses |
| `get_deadlines(days_ahead)` | Upcoming assignment/quiz deadlines |
| `list_assignments`, `get_assignment(cmid)` | Assignment details, submission status, grade and feedback |
| `get_calendar_events` | Calendar (exams, lectures, course events) |
| `get_grades(course_id?)` | Course totals or detailed grade items |
| `get_announcements`, `list_forums`, `get_forum_discussions`, `get_discussion` | Forums and news |
| `get_notifications` | Recent TUWEL notifications |
| `call_moodle_api(function, params_json)` | Raw access to any mobile-app web service function |

## Environment variables (optional)

| Variable | Meaning |
|---|---|
| `TUWEL_USERNAME`, `TUWEL_PASSWORD`, `TUWEL_TOTP_SECRET` | Credentials (override the keyring) |
| `TUWEL_TOKEN` | Use this token directly instead of `session.json` |
| `TUWEL_MCP_HOME` | Directory for the session file and downloads |
| `TUWEL_DOWNLOAD_DIR` | Where downloaded files go |
| `TUWEL_URL` | Moodle base URL (default `https://tuwel.tuwien.ac.at`) |

## Development

```bash
uv run pytest
```

Tests cover the full SSO flow against a mocked Moodle + IdP, and the tool output against
fake Moodle responses.
