import base64
import hashlib
import time

import pytest

from tuwel_mcp import auth, server
from tuwel_mcp.client import MoodleClient, _flatten
from tuwel_mcp.extract import html_to_text

SITE = "https://tuwel.tuwien.ac.at"
NOW = int(time.time())


def test_flatten_nested_params():
    assert _flatten({"courseids": [5, 7], "options": {"timestart": 1, "userevents": True}}) == {
        "courseids[0]": "5",
        "courseids[1]": "7",
        "options[timestart]": "1",
        "options[userevents]": "1",
    }


def test_decode_token_verifies_signature():
    passport = "123.456"
    sig = hashlib.md5((SITE + passport).encode()).hexdigest()
    blob = base64.b64encode(f"{sig}:::abc123:::priv".encode()).decode()
    assert auth._decode_token(blob, passport, SITE) == ("abc123", "priv")
    with pytest.raises(auth.AuthError):
        auth._decode_token(blob, "other", SITE)


def test_session_from_raw_token():
    assert auth.session_from_launch_url("0123456789abcdef0123456789abcdef", None).token.startswith("0123")


def test_file_url_adds_token():
    c = MoodleClient(auth.Session(token="tok", site_url=SITE))
    url = c.file_url(f"{SITE}/pluginfile.php/1/mod_resource/content/0/a.pdf")
    assert url == f"{SITE}/webservice/pluginfile.php/1/mod_resource/content/0/a.pdf?token=tok"


def test_html_to_text():
    assert html_to_text('<p>Hello <a href="https://x.at">link</a></p><p>Bye</p>') == "Hello link (https://x.at)\nBye"


# --------------------------------------------------------------------------- fake Moodle

RESPONSES = {
    "core_webservice_get_site_info": {"userid": 42, "fullname": "Test User", "username": "e1", "sitename": "TUWEL"},
    "core_course_get_enrolled_courses_by_timeline_classification": {
        "courses": [{"id": 10, "fullname": "Algorithms", "shortname": "ALG", "progress": 50.0, "enddate": NOW + 86400 * 90}]
    },
    "core_course_get_contents": [
        {
            "section": 0,
            "name": "General",
            "summary": "<p>Welcome</p>",
            "modules": [
                {"id": 100, "name": "Announcements", "modname": "forum", "instance": 1, "contents": []},
                {
                    "id": 101,
                    "name": "Slides week 1",
                    "modname": "resource",
                    "description": "<p>Intro slides</p>",
                    "contents": [
                        {"type": "file", "filename": "w1.pdf", "filepath": "/", "filesize": 2048,
                         "fileurl": f"{SITE}/webservice/pluginfile.php/5/mod_resource/content/1/w1.pdf"}
                    ],
                },
                {"id": 102, "name": "Exercise 1", "modname": "assign", "instance": 7,
                 "dates": [{"label": "Due:", "timestamp": NOW + 3 * 86400}]},
                {"id": 103, "name": "note", "modname": "label", "description": "<b>Exam on Friday</b>"},
            ],
        }
    ],
    "core_calendar_get_action_events_by_timesort": {
        "events": [{"name": "Exercise 1 is due", "timesort": NOW + 3 * 86400, "modulename": "assign",
                    "url": f"{SITE}/mod/assign/view.php?id=102", "course": {"fullname": "Algorithms"},
                    "action": {"name": "Add submission", "actionable": True}}]
    },
    "mod_assign_get_assignments": {
        "courses": [{"id": 10, "fullname": "Algorithms", "assignments": [
            {"id": 7, "cmid": 102, "name": "Exercise 1", "duedate": NOW + 3 * 86400, "intro": "<p>Solve it</p>",
             "introattachments": [{"filename": "sheet.pdf", "filesize": 100, "fileurl": f"{SITE}/x/sheet.pdf"}]}
        ]}]
    },
    "mod_assign_get_submission_status": {"lastattempt": {"submission": {"status": "new"}, "gradingstatus": "notgraded"}},
    "core_course_get_course_module": {"cm": {"id": 102, "course": 10, "modname": "assign", "instance": 7, "name": "Exercise 1"}},
    "gradereport_user_get_grade_items": {"usergrades": [{"gradeitems": [
        {"itemname": "Exercise 1", "gradeformatted": "8,00", "rangeformatted": "0-10", "percentageformatted": "80 %"},
        {"itemtype": "course", "gradeformatted": "8,00"},
    ]}]},
    "mod_forum_get_forums_by_courses": [{"id": 1, "course": 10, "name": "Announcements", "type": "news"}],
    "mod_forum_get_forum_discussions": {"discussions": [
        {"name": "Room change", "discussion": 55, "userfullname": "Prof", "created": NOW - 3600, "message": "<p>Now in HS1</p>"}
    ]},
}


class FakeClient:
    def __init__(self):
        self.session = auth.Session(token="tok", site_url=SITE, userid=42)
        self.calls = []

    async def call(self, function, **params):
        self.calls.append((function, params))
        return RESPONSES[function]


@pytest.fixture
def fake(monkeypatch):
    fc = FakeClient()

    async def get_client():
        return fc

    monkeypatch.setattr(server, "client", get_client)
    monkeypatch.setattr(server.S, "site_info", RESPONSES["core_webservice_get_site_info"])
    server.S.contents_cache.clear()
    server.S.courses_cache = None
    return fc


async def test_list_courses(fake):
    out = await server.list_courses()
    assert "[10] Algorithms (ALG)" in out and "progress 50%" in out


async def test_course_contents(fake):
    out = await server.get_course_contents(10)
    assert "# Algorithms (course id 10)" in out
    assert "[resource] Slides week 1 (cmid 101)" in out
    assert "file: w1.pdf (2.0 KB)" in out
    assert "(note) Exam on Friday" in out
    assert "Due" in out


async def test_deadlines(fake):
    out = await server.get_deadlines()
    assert "Exercise 1 is due" in out and "(cmid 102)" in out and "Add submission" in out


async def test_assignment(fake):
    out = await server.get_assignment(102)
    assert "Solve it" in out and "sheet.pdf" in out and "Submission status: new" in out


async def test_grades(fake):
    out = await server.get_grades(10)
    assert "Exercise 1: 8,00, range 0-10, 80 %" in out and "Course total" in out


async def test_announcements(fake):
    out = await server.get_announcements()
    assert "Room change" in out and "Now in HS1" in out


async def test_search(fake):
    out = await server.search_courses_content("slides w1.pdf")
    assert "Slides week 1 (cmid 101)" in out


async def test_auth_error_message(monkeypatch):
    async def get_client():
        raise auth.AuthError("Not logged in to TUWEL.")

    monkeypatch.setattr(server, "client", get_client)
    monkeypatch.setattr(server, "_auto_relogin", lambda: _false())
    out = await server.list_courses()
    assert out.startswith("Authentication required") and "tuwel_login" in out


async def _false():
    return False


# --------------------------------------------------------------------------- mocked SSO flow


def test_full_sso_flow(respx_mock):
    import httpx

    idp = "https://idp.zid.tuwien.ac.at/simplesaml/module.php/core/loginuserpass?AuthState=x"
    state = {"logged_in": False, "posted": None}

    def launch(request):
        if not state["logged_in"]:
            state["launch"] = str(request.url)
            return httpx.Response(303, headers={"location": f"{SITE}/login/index.php"})
        passport = request.url.params["passport"]
        sig = hashlib.md5((SITE + passport).encode()).hexdigest()
        blob = base64.b64encode(f"{sig}:::TOKEN123:::PRIV".encode()).decode()
        return httpx.Response(303, headers={"location": f"moodlemobile://token={blob}"})

    def idp_post(request):
        state["posted"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, html=(
            f'<form method="post" action="{SITE}/auth/saml2/sp/saml2-acs.php/tuwel">'
            '<input type="hidden" name="SAMLResponse" value="abc"><input type="hidden" name="RelayState" value="r">'
            "</form>"))

    def acs(request):
        state["logged_in"] = True
        return httpx.Response(303, headers={"location": state["launch"]})

    respx_mock.get(url__startswith=f"{SITE}/admin/tool/mobile/launch.php").mock(side_effect=launch)
    respx_mock.get(f"{SITE}/login/index.php").respond(
        200, html=f'<a href="{SITE}/auth/saml2/login.php?wants=x&amp;idp=1">TU Wien Login</a>')
    respx_mock.get(url__startswith=f"{SITE}/auth/saml2/login.php").respond(302, headers={"location": idp})
    respx_mock.get(idp).respond(200, html=(
        f'<form id="f" action="{idp}" method="post"><input name="username"><input name="password" type="password">'
        '<input name="totp"><input type="hidden" name="AuthState" value="st"></form>'))
    respx_mock.post(idp).mock(side_effect=idp_post)
    respx_mock.post(f"{SITE}/auth/saml2/sp/saml2-acs.php/tuwel").mock(side_effect=acs)

    session = auth.login("e1", "pw", "123456", SITE)
    assert (session.token, session.privatetoken) == ("TOKEN123", "PRIV")
    assert state["posted"] == {"username": "e1", "password": "pw", "totp": "123456", "AuthState": "st"}
