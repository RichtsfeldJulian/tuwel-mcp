"""MCP server exposing TUWEL (TU Wien Moodle) to AI agents."""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import auth, config
from .auth import AuthError, Session
from .client import MoodleClient, MoodleError
from .extract import extract_text, html_to_text

mcp = MCPServer(
    "tuwel",
    instructions=(
        "Access to the user's TU Wien TUWEL (Moodle) account: courses, materials, files, "
        "assignments, deadlines, grades, forums/announcements and calendar. Typical workflow: "
        "list_courses -> get_course_contents -> read_module / read_file. Use get_deadlines for "
        "anything about due dates. If a tool reports that authentication is required, ask the "
        "user for their current 6-digit TU Wien 2FA code and call tuwel_login with it."
    ),
)

AUTH_HELP = (
    "Ask the user for the current 6-digit 2FA code from their authenticator app and call "
    "tuwel_login(totp_code=...). If no credentials are stored yet, the user must run "
    "`tuwel-mcp login` once in a terminal."
)

# --------------------------------------------------------------------------- #
# State & helpers
# --------------------------------------------------------------------------- #


class _State:
    client: MoodleClient | None = None
    site_info: dict | None = None
    contents_cache: dict[int, tuple[float, list]] = {}
    courses_cache: tuple[float, list] | None = None
    relogin_lock = asyncio.Lock()


S = _State()
CACHE_TTL = 600


async def _reset(session: Session | None = None) -> None:
    if S.client:
        await S.client.aclose()
    S.client = MoodleClient(session) if session else None
    S.site_info = None
    S.contents_cache.clear()
    S.courses_cache = None


async def client() -> MoodleClient:
    if S.client is None:
        session = Session.load()
        if session is None:
            raise AuthError("Not logged in to TUWEL.")
        S.client = MoodleClient(session)
    return S.client


async def site_info() -> dict:
    if S.site_info is None:
        c = await client()
        S.site_info = await c.call("core_webservice_get_site_info")
        sess = c.session
        if sess.userid != S.site_info.get("userid") and not _env_token():
            sess.userid = S.site_info.get("userid")
            sess.fullname = S.site_info.get("fullname")
            sess.username = sess.username or S.site_info.get("username")
            sess.save()
    return S.site_info


def _env_token() -> bool:
    import os

    return bool(os.environ.get("TUWEL_TOKEN"))


async def _auto_relogin() -> bool:
    """Silently renew the token if username, password and TOTP secret are all stored."""
    if not (
        auth.get_credential("username")
        and auth.get_credential("password")
        and auth.get_credential("totp_secret")
    ):
        return False
    async with S.relogin_lock:
        try:
            session = await asyncio.to_thread(auth.login_with_stored_credentials)
        except AuthError:
            return False
        session.save()
        await _reset(session)
        return True


def tool(fn=None, *, read_only: bool = True):
    """Register an MCP tool with uniform auth/error handling."""
    if fn is None:
        return functools.partial(tool, read_only=read_only)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            try:
                return await fn(*args, **kwargs)
            except AuthError:
                if await _auto_relogin():
                    return await fn(*args, **kwargs)
                raise
        except AuthError as exc:
            return f"Authentication required: {exc}\n{AUTH_HELP}"
        except MoodleError as exc:
            return f"TUWEL error: {exc}"

    return mcp.tool(annotations=ToolAnnotations(readOnlyHint=read_only, openWorldHint=True))(wrapper)


def fmt_time(ts: int | None, relative: bool = True) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts).astimezone()
    out = dt.strftime("%a %Y-%m-%d %H:%M")
    if relative:
        delta = ts - time.time()
        days = abs(delta) / 86400
        if days < 1:
            span = f"{abs(delta) / 3600:.0f}h"
        else:
            span = f"{days:.0f}d"
        out += f" (in {span})" if delta >= 0 else f" ({span} ago)"
    return out


def fmt_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


async def courses(classification: str = "all") -> list[dict]:
    if classification == "all" and S.courses_cache and time.time() - S.courses_cache[0] < CACHE_TTL:
        return S.courses_cache[1]
    c = await client()
    try:
        res = await c.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
            limit=0,
            offset=0,
        )
        result = res.get("courses", [])
    except MoodleError:
        info = await site_info()
        result = await c.call("core_enrol_get_users_courses", userid=info["userid"])
    if classification == "all":
        S.courses_cache = (time.time(), result)
    return result


async def course_contents(course_id: int) -> list[dict]:
    cached = S.contents_cache.get(course_id)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    c = await client()
    data = await c.call("core_course_get_contents", courseid=course_id)
    S.contents_cache[course_id] = (time.time(), data)
    return data


async def course_name(course_id: int) -> str:
    for course in await courses():
        if course["id"] == course_id:
            return course.get("fullname") or course.get("shortname") or str(course_id)
    return str(course_id)


def _module_dates(mod: dict) -> str:
    dates = mod.get("dates") or []
    return "; ".join(f"{d.get('label', '').rstrip(':')} {fmt_time(d.get('timestamp'), False)}" for d in dates)


# --------------------------------------------------------------------------- #
# Auth tools
# --------------------------------------------------------------------------- #


@mcp.tool()
async def tuwel_status() -> str:
    """Check whether the TUWEL connection works and show the logged-in user."""
    session = Session.load()
    if session is None:
        return f"Not logged in. {AUTH_HELP}"
    try:
        info = await site_info()
    except AuthError as exc:
        if await _auto_relogin():
            info = await site_info()
        else:
            return f"Stored token is invalid or expired ({exc}). {AUTH_HELP}"
    age_days = (time.time() - session.created) / 86400
    auto = all(auth.get_credential(k) for k in ("username", "password", "totp_secret"))
    return (
        f"Connected to {info.get('sitename')} ({session.site_url}) as {info.get('fullname')} "
        f"({info.get('username')}, user id {info.get('userid')}).\n"
        f"Token age: {age_days:.1f} days. Automatic renewal: {'enabled' if auto else 'disabled (needs 2FA code)'}."
    )


@mcp.tool()
async def tuwel_login(totp_code: str | None = None) -> str:
    """Log in to TUWEL again (when the token has expired) using the stored username/password.

    Args:
        totp_code: Current 6-digit code from the user's TU Wien 2FA authenticator app.
            Can be omitted only if a TOTP secret is stored for automatic login.
    """
    if totp_code is not None and not re.fullmatch(r"\s*\d{6}\s*", totp_code):
        return "totp_code must be the 6-digit code from the authenticator app."
    try:
        session = await asyncio.to_thread(auth.login_with_stored_credentials, totp_code)
    except AuthError as exc:
        return f"Login failed: {exc}"
    session.save()
    await _reset(session)
    info = await site_info()
    return f"Logged in to TUWEL as {info.get('fullname')} ({info.get('username')})."


# --------------------------------------------------------------------------- #
# Courses & content
# --------------------------------------------------------------------------- #


@tool
async def list_courses(status: str = "inprogress") -> str:
    """List the user's enrolled TUWEL courses.

    Args:
        status: 'inprogress' (current semester, default), 'past', 'future' or 'all'.
    """
    if status not in ("inprogress", "past", "future", "all"):
        return "status must be one of inprogress, past, future, all"
    items = await courses(status)
    if not items:
        return f"No {status} courses found."
    lines = [f"{len(items)} {status} course(s):"]
    for c in sorted(items, key=lambda c: c.get("fullname", "")):
        extra = []
        if c.get("progress") is not None:
            extra.append(f"progress {c['progress']:.0f}%")
        if c.get("enddate"):
            extra.append(f"ends {fmt_time(c['enddate'], False)[4:14]}")
        if c.get("isfavourite"):
            extra.append("starred")
        lines.append(
            f"- [{c['id']}] {c.get('fullname')} ({c.get('shortname')})"
            + (f" - {', '.join(extra)}" if extra else "")
        )
    return "\n".join(lines)


@tool
async def get_course_contents(
    course_id: int, include_descriptions: bool = True, section: str | None = None
) -> str:
    """Show the structure of a course: sections, activities/resources (with cmid) and attached files.

    Args:
        course_id: Course id from list_courses.
        include_descriptions: Include section summaries and activity descriptions (truncated).
        section: Optional case-insensitive filter on the section name or number.
    """
    data = await course_contents(course_id)
    out = [f"# {await course_name(course_id)} (course id {course_id})"]
    for sec in data:
        name = sec.get("name") or f"Section {sec.get('section')}"
        if section and section.lower() not in name.lower() and section != str(sec.get("section")):
            continue
        modules = sec.get("modules", [])
        summary = html_to_text(sec.get("summary")) if include_descriptions else ""
        if not modules and not summary:
            continue
        out.append(f"\n## {name}")
        if summary:
            out.append(clip(summary, 800))
        for mod in modules:
            if mod.get("modname") == "label":
                text = html_to_text(mod.get("description"))
                if text and include_descriptions:
                    out.append(f"- (note) {clip(text, 400)}")
                continue
            line = f"- [{mod.get('modname')}] {mod.get('name')} (cmid {mod.get('id')})"
            if mod.get("visible") == 0 or mod.get("uservisible") is False:
                line += " [not available]"
            dates = _module_dates(mod)
            if dates:
                line += f" - {dates}"
            out.append(line)
            if include_descriptions and mod.get("description"):
                out.append(f"  {clip(html_to_text(mod['description']), 400)}")
            for f in mod.get("contents") or []:
                if f.get("type") == "url":
                    out.append(f"  - link: {f.get('fileurl')}")
                elif f.get("type") == "file":
                    path = (f.get("filepath") or "/").strip("/")
                    fname = f"{path}/{f.get('filename')}" if path else f.get("filename")
                    out.append(f"  - file: {fname} ({fmt_size(f.get('filesize'))}) url: {f.get('fileurl')}")
    return "\n".join(out)


@tool
async def read_module(cmid: int, max_chars: int = 30000) -> str:
    """Read an activity/resource by its course module id (cmid): page text, file contents,
    assignment details, quiz info, link target, folder listing or forum discussions.

    Args:
        cmid: Course module id (shown as 'cmid' in get_course_contents).
        max_chars: Maximum characters of document text to return.
    """
    c = await client()
    cm = (await c.call("core_course_get_course_module", cmid=cmid))["cm"]
    modname, course_id, instance = cm["modname"], cm["course"], cm["instance"]
    header = f"# {cm['name']} [{modname}] in {await course_name(course_id)}"

    mod = None
    for sec in await course_contents(course_id):
        for m in sec.get("modules", []):
            if m.get("id") == cmid:
                mod = m
    description = html_to_text(mod.get("description")) if mod else ""
    files = [f for f in (mod.get("contents") or []) if f.get("type") == "file"] if mod else []

    if modname == "assign":
        return await _assignment_details(instance, course_id, header)
    if modname == "page":
        res = await c.call("mod_page_get_pages_by_courses", courseids=[course_id])
        page = next((p for p in res.get("pages", []) if p.get("coursemodule") == cmid), None)
        if page:
            body = html_to_text(page.get("intro")) + "\n\n" + html_to_text(page.get("content"))
            return f"{header}\n\n{clip(body, max_chars)}"
    if modname == "quiz":
        res = await c.call("mod_quiz_get_quizzes_by_courses", courseids=[course_id])
        q = next((q for q in res.get("quizzes", []) if q.get("coursemodule") == cmid), None)
        if q:
            return "\n".join([
                header,
                f"Opens: {fmt_time(q.get('timeopen'))}",
                f"Closes: {fmt_time(q.get('timeclose'))}",
                f"Time limit: {q['timelimit'] // 60} min" if q.get("timelimit") else "Time limit: none",
                f"Attempts allowed: {q.get('attempts') or 'unlimited'}",
                "",
                html_to_text(q.get("intro")),
            ])
    if modname == "forum":
        return header + "\n\n" + await _forum_discussions(instance, 15)
    if modname == "url":
        links = [f.get("fileurl") for f in (mod or {}).get("contents") or []]
        return f"{header}\nLink: {', '.join(l for l in links if l) or '?'}\n\n{description}"
    if modname == "resource" and files:
        text = await _read_file(files[0]["fileurl"], 0, max_chars, files[0].get("filename"))
        return f"{header}\n{description}\n\n{text}"
    if files:
        listing = "\n".join(
            f"- {(f.get('filepath') or '/').strip('/') + '/' if (f.get('filepath') or '/') != '/' else ''}"
            f"{f.get('filename')} ({fmt_size(f.get('filesize'))}) url: {f.get('fileurl')}"
            for f in files
        )
        return f"{header}\n{description}\n\nFiles (use read_file with a url):\n{listing}"
    return f"{header}\n{_module_dates(mod) if mod else ''}\n\n{description or '(no text content available via the API)'}\nWeb: {cm.get('url') or ''}"


_file_cache: dict[str, Path] = {}


async def _read_file(url: str, offset: int, max_chars: int, filename: str | None = None) -> str:
    c = await client()
    path = _file_cache.get(url)
    if path is None or not path.exists():
        parts = [p for p in urlparse(url).path.split("/") if p]
        sub = parts[parts.index("pluginfile.php") + 1] if "pluginfile.php" in parts else "misc"
        path = await c.download(url, config.download_dir() / sub, filename)
        _file_cache[url] = path
    text = await asyncio.to_thread(extract_text, path)
    if text is None:
        return f"Downloaded to {path} ({fmt_size(path.stat().st_size)}), but text extraction is not supported for this file type."
    total = len(text)
    chunk = text[offset : offset + max_chars]
    footer = f"\n\n[chars {offset}-{offset + len(chunk)} of {total}; saved at {path}]"
    if offset + max_chars < total:
        footer += f" - call read_file again with offset={offset + max_chars} for more."
    return chunk + footer


@tool
async def read_file(url: str, offset: int = 0, max_chars: int = 30000) -> str:
    """Download a TUWEL file (PDF, DOCX, PPTX, text/code, HTML) and return its text content.
    The file is also saved locally; the local path is reported.

    Args:
        url: File url as shown by get_course_contents / read_module (a tuwel.tuwien.ac.at pluginfile url).
        offset: Character offset for paging through long documents.
        max_chars: Maximum characters to return.
    """
    return await _read_file(url, offset, max_chars)


@tool(read_only=False)
async def download_course_files(course_id: int, dest_dir: str | None = None, section: str | None = None) -> str:
    """Download all files of a course (optionally one section) to a local folder, preserving
    the section structure. Useful for working with materials locally.

    Args:
        course_id: Course id.
        dest_dir: Target directory (default: the tuwel-mcp downloads folder).
        section: Optional case-insensitive filter on section name.
    """
    c = await client()
    cname = re.sub(r'[<>:"/\\|?*]', "_", await course_name(course_id))[:80]
    root = Path(dest_dir).expanduser() if dest_dir else config.download_dir() / cname
    saved, failed = [], []
    for sec in await course_contents(course_id):
        sname = sec.get("name") or f"Section {sec.get('section')}"
        if section and section.lower() not in sname.lower():
            continue
        sdir = root / re.sub(r'[<>:"/\\|?*]', "_", sname)[:80]
        for mod in sec.get("modules", []):
            for f in mod.get("contents") or []:
                if f.get("type") != "file" or not f.get("fileurl"):
                    continue
                sub = (f.get("filepath") or "/").strip("/")
                target_dir = sdir / sub if sub else sdir
                if mod.get("modname") == "folder":
                    target_dir = sdir / re.sub(r'[<>:"/\\|?*]', "_", mod.get("name", "folder"))[:80] / sub
                try:
                    path = await c.download(f["fileurl"], target_dir, f.get("filename"))
                    saved.append(str(path.relative_to(root)))
                except Exception as exc:  # keep going on individual failures
                    failed.append(f"{f.get('filename')}: {exc}")
    out = [f"Downloaded {len(saved)} file(s) to {root}"]
    out += [f"- {p}" for p in saved[:200]]
    if len(saved) > 200:
        out.append(f"... and {len(saved) - 200} more")
    if failed:
        out.append(f"\n{len(failed)} failed:")
        out += [f"- {f}" for f in failed]
    return "\n".join(out)


@tool
async def search_courses_content(query: str, course_id: int | None = None) -> str:
    """Search section names, activity names, descriptions and file names across your current
    courses (or one course). All query words must match (case-insensitive).

    Args:
        query: Search words, e.g. 'exercise 3 pdf' or 'exam'.
        course_id: Restrict to one course.
    """
    words = query.lower().split()
    ids = [course_id] if course_id else [c["id"] for c in await courses("inprogress")]
    data = await asyncio.gather(*(course_contents(i) for i in ids), return_exceptions=True)
    hits = []
    for cid, contents in zip(ids, data):
        if isinstance(contents, Exception):
            continue
        cname = await course_name(cid)
        for sec in contents:
            sname = sec.get("name") or ""
            for mod in sec.get("modules", []):
                files = " ".join(f.get("filename", "") for f in mod.get("contents") or [])
                hay = " ".join([sname, mod.get("name", ""), html_to_text(mod.get("description")), files]).lower()
                if all(w in hay for w in words):
                    hits.append(f"- {cname} > {sname} > [{mod.get('modname')}] {mod.get('name')} (cmid {mod.get('id')})")
    if not hits:
        return f"No matches for '{query}'."
    return f"{len(hits)} match(es) for '{query}':\n" + "\n".join(hits[:100])


# --------------------------------------------------------------------------- #
# Deadlines, assignments, calendar
# --------------------------------------------------------------------------- #


@tool
async def get_deadlines(days_ahead: int = 30, include_overdue_days: int = 0, course_id: int | None = None) -> str:
    """List upcoming deadlines (assignments, quizzes, other actionable events) sorted by due date.

    Args:
        days_ahead: How many days into the future to look.
        include_overdue_days: Also include items that became due in the last N days.
        course_id: Restrict to one course.
    """
    c = await client()
    now = int(time.time())
    params = dict(
        timesortfrom=now - include_overdue_days * 86400,
        timesortto=now + days_ahead * 86400,
        limitnum=100,
    )
    if course_id:
        res = await c.call("core_calendar_get_action_events_by_course", courseid=course_id, **params)
    else:
        res = await c.call("core_calendar_get_action_events_by_timesort", **params)
    events = res.get("events", [])
    if not events:
        return f"No deadlines in the next {days_ahead} days."
    lines = [f"{len(events)} deadline(s):"]
    for e in sorted(events, key=lambda e: e.get("timesort", 0)):
        course = (e.get("course") or {}).get("fullname", "")
        action = e.get("action") or {}
        status = ""
        if action.get("name"):
            status = f" - action: {action['name']}" + ("" if action.get("actionable", True) else " (not actionable)")
        if e.get("overdue"):
            status += " - OVERDUE"
        m = re.search(r"[?&]id=(\d+)", e.get("url") or "")
        ref = f" (cmid {m.group(1)})" if m and e.get("modulename") else ""
        lines.append(
            f"- {fmt_time(e.get('timesort'))}: {e.get('name')} [{e.get('modulename') or e.get('eventtype')}] "
            f"- {course}{ref}{status}"
        )
    return "\n".join(lines)


async def _assignment_details(assign_id: int, course_id: int, header: str | None = None) -> str:
    c = await client()
    res = await c.call("mod_assign_get_assignments", courseids=[course_id])
    a = next(
        (a for course in res.get("courses", []) for a in course.get("assignments", []) if a["id"] == assign_id),
        None,
    )
    if a is None:
        return f"Assignment {assign_id} not found."
    out = [header or f"# {a['name']} (assignment {assign_id}, cmid {a.get('cmid')})"]
    out += [
        f"Opens: {fmt_time(a.get('allowsubmissionsfromdate'))}",
        f"Due: {fmt_time(a.get('duedate'))}",
    ]
    if a.get("cutoffdate"):
        out.append(f"Cut-off: {fmt_time(a['cutoffdate'])}")
    if a.get("gradingduedate"):
        out.append(f"Grading due: {fmt_time(a['gradingduedate'], False)}")
    if a.get("grade"):
        out.append(f"Max grade: {a['grade']}")
    out.append("\n" + html_to_text(a.get("intro")))
    attachments = a.get("introattachments") or []
    if attachments:
        out.append("\nAttachments (use read_file):")
        out += [f"- {f.get('filename')} ({fmt_size(f.get('filesize'))}) url: {f.get('fileurl')}" for f in attachments]
    try:
        st = await c.call("mod_assign_get_submission_status", assignid=assign_id)
        last = st.get("lastattempt") or {}
        sub = last.get("submission") or last.get("teamsubmission") or {}
        out.append(
            f"\nSubmission status: {sub.get('status', 'none')}"
            f" | grading: {last.get('gradingstatus', '?')}"
            + (f" | last modified {fmt_time(sub.get('timemodified'), False)}" if sub.get("timemodified") else "")
        )
        for plugin in sub.get("plugins") or []:
            for area in plugin.get("fileareas") or []:
                for f in area.get("files") or []:
                    out.append(f"- submitted file: {f.get('filename')} url: {f.get('fileurl')}")
        fb = st.get("feedback") or {}
        if fb:
            out.append(f"Grade: {fb.get('gradefordisplay') and html_to_text(fb['gradefordisplay'])}")
            for plugin in fb.get("plugins") or []:
                for ed in plugin.get("editorfields") or []:
                    if ed.get("text"):
                        out.append(f"Feedback: {html_to_text(ed['text'])}")
                for area in plugin.get("fileareas") or []:
                    for f in area.get("files") or []:
                        out.append(f"- feedback file: {f.get('filename')} url: {f.get('fileurl')}")
    except MoodleError:
        pass
    return "\n".join(out)


@tool
async def list_assignments(course_id: int | None = None, only_open: bool = False) -> str:
    """List assignments with due dates for one course or all current courses.

    Args:
        course_id: Restrict to one course (default: all in-progress courses).
        only_open: Only show assignments whose due date is in the future (or have none).
    """
    c = await client()
    ids = [course_id] if course_id else [x["id"] for x in await courses("inprogress")]
    res = await c.call("mod_assign_get_assignments", courseids=ids)
    now = time.time()
    lines = []
    for course in res.get("courses", []):
        items = course.get("assignments", [])
        if only_open:
            items = [a for a in items if not a.get("duedate") or a["duedate"] > now]
        if not items:
            continue
        lines.append(f"\n## {course.get('fullname')} (course id {course.get('id')})")
        for a in sorted(items, key=lambda a: a.get("duedate") or 0):
            lines.append(f"- {a['name']} (assignment id {a['id']}, cmid {a.get('cmid')}) - due {fmt_time(a.get('duedate'))}")
    return "\n".join(lines).strip() or "No assignments found."


@tool
async def get_assignment(cmid: int) -> str:
    """Full details of an assignment: description, attachments, dates, submission status, grade & feedback.

    Args:
        cmid: Course module id of the assignment.
    """
    c = await client()
    cm = (await c.call("core_course_get_course_module", cmid=cmid))["cm"]
    if cm["modname"] != "assign":
        return f"cmid {cmid} is a '{cm['modname']}', not an assignment. Use read_module instead."
    return await _assignment_details(cm["instance"], cm["course"])


@tool
async def get_calendar_events(days_ahead: int = 14, days_back: int = 0, course_id: int | None = None) -> str:
    """List calendar events (lectures, exams, course/site/user events and due dates) in a time window.

    Args:
        days_ahead: Days into the future.
        days_back: Days into the past.
        course_id: Restrict to one course.
    """
    c = await client()
    now = int(time.time())
    ids = [course_id] if course_id else [x["id"] for x in await courses("inprogress")]
    res = await c.call(
        "core_calendar_get_calendar_events",
        events={"courseids": ids},
        options={
            "userevents": 0 if course_id else 1,
            "siteevents": 0 if course_id else 1,
            "timestart": now - days_back * 86400,
            "timeend": now + days_ahead * 86400,
        },
    )
    events = sorted(res.get("events", []), key=lambda e: e.get("timestart", 0))
    if not events:
        return "No calendar events in this window."
    names = {x["id"]: x.get("fullname") for x in await courses()}
    lines = []
    for e in events:
        dur = f" ({e['timeduration'] // 60} min)" if e.get("timeduration") else ""
        where = f" @ {e['location']}" if e.get("location") else ""
        course = names.get(e.get("courseid"), "")
        lines.append(f"- {fmt_time(e.get('timestart'))}{dur}: {e.get('name')} [{e.get('eventtype')}] {course}{where}")
        desc = html_to_text(e.get("description"))
        if desc:
            lines.append(f"  {clip(desc, 300)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Grades
# --------------------------------------------------------------------------- #


@tool
async def get_grades(course_id: int | None = None) -> str:
    """Show grades. Without course_id: overview of course totals. With course_id: all grade items
    with points, percentages, ranges and feedback.

    Args:
        course_id: Course id for the detailed grade report.
    """
    c = await client()
    if course_id is None:
        res = await c.call("gradereport_overview_get_course_grades")
        names = {x["id"]: x.get("fullname") for x in await courses()}
        lines = ["Course totals:"]
        for g in res.get("grades", []):
            lines.append(f"- {names.get(g['courseid'], g['courseid'])} (course id {g['courseid']}): {g.get('grade') or '-'}")
        return "\n".join(lines)
    info = await site_info()
    res = await c.call("gradereport_user_get_grade_items", courseid=course_id, userid=info["userid"])
    ug = (res.get("usergrades") or [{}])[0]
    lines = [f"# Grades: {await course_name(course_id)}"]
    for item in ug.get("gradeitems", []):
        name = item.get("itemname") or ("Course total" if item.get("itemtype") == "course" else item.get("itemtype"))
        parts = [f"{item.get('gradeformatted') or '-'}"]
        if item.get("rangeformatted"):
            parts.append(f"range {item['rangeformatted']}")
        if item.get("percentageformatted") and item["percentageformatted"] != "-":
            parts.append(item["percentageformatted"])
        if item.get("weightformatted") and item["weightformatted"] != "-":
            parts.append(f"weight {item['weightformatted']}")
        lines.append(f"- {name}: {', '.join(parts)}")
        fb = html_to_text(item.get("feedback"))
        if fb:
            lines.append(f"  feedback: {clip(fb, 500)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Forums / announcements / notifications
# --------------------------------------------------------------------------- #


async def _forum_discussions(forum_id: int, limit: int) -> str:
    c = await client()
    res = await c.call("mod_forum_get_forum_discussions", forumid=forum_id, sortorder=-1, page=0, perpage=limit)
    ds = res.get("discussions", [])
    if not ds:
        return "No discussions."
    lines = []
    for d in ds:
        lines.append(
            f"- {d.get('name')} (discussion id {d.get('discussion')}) by {d.get('userfullname')}, "
            f"{fmt_time(d.get('created'), False)}, {d.get('numreplies', 0)} replies"
        )
        msg = html_to_text(d.get("message"))
        if msg:
            lines.append(f"  {clip(msg, 300)}")
    return "\n".join(lines)


@tool
async def get_announcements(course_id: int | None = None, limit: int = 5) -> str:
    """Latest announcements (news forum posts) for one course or all current courses.

    Args:
        course_id: Restrict to one course.
        limit: Number of announcements per course.
    """
    c = await client()
    ids = [course_id] if course_id else [x["id"] for x in await courses("inprogress")]
    forums = await c.call("mod_forum_get_forums_by_courses", courseids=ids)
    news = [f for f in forums if f.get("type") == "news"]
    if not news:
        return "No announcement forums found."
    names = {x["id"]: x.get("fullname") for x in await courses()}
    results = await asyncio.gather(*(_forum_discussions(f["id"], limit) for f in news), return_exceptions=True)
    out = []
    for f, res in zip(news, results):
        if isinstance(res, Exception) or res == "No discussions.":
            continue
        out.append(f"## {names.get(f['course'], f['course'])} - {f.get('name')} (forum id {f['id']})\n{res}")
    return "\n\n".join(out) or "No announcements."


@tool
async def list_forums(course_id: int) -> str:
    """List all forums of a course.

    Args:
        course_id: Course id.
    """
    c = await client()
    forums = await c.call("mod_forum_get_forums_by_courses", courseids=[course_id])
    if not forums:
        return "No forums."
    return "\n".join(
        f"- {f.get('name')} (forum id {f['id']}, cmid {f.get('cmid')}, type {f.get('type')}, "
        f"{f.get('numdiscussions', '?')} discussions)"
        for f in forums
    )


@tool
async def get_forum_discussions(forum_id: int, limit: int = 20) -> str:
    """List the newest discussions of a forum.

    Args:
        forum_id: Forum id (from list_forums / get_announcements).
        limit: Max discussions.
    """
    return await _forum_discussions(forum_id, limit)


@tool
async def get_discussion(discussion_id: int) -> str:
    """Read all posts of a forum discussion thread.

    Args:
        discussion_id: Discussion id.
    """
    c = await client()
    res = await c.call("mod_forum_get_discussion_posts", discussionid=discussion_id, sortby="created", sortdirection="ASC")
    lines = []
    for p in res.get("posts", []):
        author = (p.get("author") or {}).get("fullname", "?")
        lines.append(f"### {p.get('subject')} - {author}, {fmt_time(p.get('timecreated'), False)}")
        lines.append(html_to_text(p.get("message")))
        for a in p.get("attachments") or []:
            lines.append(f"- attachment: {a.get('filename')} url: {a.get('fileurl')}")
    return "\n\n".join(lines) or "No posts."


@tool
async def get_notifications(limit: int = 20, unread_only: bool = False) -> str:
    """Recent TUWEL notifications (new grades, forum posts, assignment reminders, ...).

    Args:
        limit: Max number of notifications.
        unread_only: Only unread notifications.
    """
    c = await client()
    info = await site_info()
    res = await c.call("message_popup_get_popup_notifications", useridto=info["userid"], limit=limit, offset=0, newestfirst=1)
    items = res.get("notifications", [])
    if unread_only:
        items = [n for n in items if not n.get("read")]
    if not items:
        return "No notifications."
    return "\n".join(
        f"- {'' if n.get('read') else '[unread] '}{fmt_time(n.get('timecreated'), False)}: {n.get('subject')}"
        + (f" ({n['contexturl']})" if n.get("contexturl") else "")
        for n in items
    )


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #


@tool(read_only=False)
async def call_moodle_api(function: str, params_json: str = "{}") -> str:
    """Call any Moodle web service function available to the mobile app service and return raw JSON.
    Use only when no dedicated tool fits. Read-only functions are strongly preferred.

    Args:
        function: Moodle web service function name, e.g. 'core_course_get_courses_by_field'.
        params_json: JSON object with the parameters, e.g. '{"field": "id", "value": 123}'.
    """
    try:
        params: dict[str, Any] = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        return f"params_json is not valid JSON: {exc}"
    c = await client()
    result = await c.call(function, **params)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    return clip(text, 60000)


def run() -> None:
    mcp.run()
