"""Web semantics are local validation and detached DB reads, never browser work."""

import sqlite3
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import event

from postcardscene import domain as d
from postcardscene.web_selection import (
    WebSelectionError,
    resolve_web_target,
    validate_web_configuration,
    validate_web_source,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Web semantics performed network/browser work")

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/Case%2fPath?share=private%20id#Map",
        "http://localhost",
        "http://wayfarer.local:8080/display",
        "http://127.0.0.1:80",
        "http://192.168.1.5",
        "http://169.254.1.2",
        "http://[::1]:8080/",
        "http://[::1]:00080/",
        "https://EXAMPLE.invalid/",
        "http://host/" + "a" * 8180,
    ],
)
def test_valid_url(url):
    assert validate_web_source("web_url", {"url": "  " + url + "  "}) == {"url": url}


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,x",
        "blob:https://host/x",
        "chrome://settings",
        "chrome-extension://id",
        "devtools://devtools",
        "ftp://host",
        "//host/path",
        "https:///path",
        "http://",
        "http://user@host",
        "http://u:p@host",
        "http://host:0",
        "http://host:65536",
        "http://host:abc",
        "http://host:",
        "http://[bad]/",
        "http://[::1]suffix/",
        "http://bad<host/",
        "http://bad%zzhost/",
        "http://host/a b",
        "http://host/\tpath",
        "http://host/\npath",
        "http://host/\x00",
        "http://host/\x7f",
        "http://host/\\path",
        "http://host/\u2003path",
        "x" * 8193,
        "",
        None,
        1,
    ],
)
def test_invalid_url(url):
    with pytest.raises(WebSelectionError) as error:
        validate_web_source("web_url", {"url": url})
    if isinstance(url, str) and len(url) > 10:
        assert url not in str(error.value)


@pytest.mark.parametrize("config", [None, [], {}, {"url": "http://host", "token": "x"}])
def test_exact_source_configuration(config):
    with pytest.raises(WebSelectionError):
        validate_web_source("web_url", config)
    with pytest.raises(WebSelectionError):
        validate_web_source("local_directory", {"url": "http://host"})


@pytest.mark.parametrize(
    "kind,config",
    [("video", {}), ("web_view", None), ("web_view", []), ("web_view", {"reload": 5})],
)
def test_exact_widget_configuration(kind, config):
    assert validate_web_configuration("web_view", {}) == {}
    with pytest.raises(WebSelectionError):
        validate_web_configuration(kind, config)


@pytest.fixture
def web_target(catalog):
    db, _, _, _ = catalog
    with db.transaction() as session:
        source_id = d.create_source(
            session,
            name="Web",
            kind="web_url",
            configuration={"url": "http://host/?share=private"},
        ).id
        widget_id = d.create_widget(
            session, name="Web", kind="web_view", configuration={}, source_id=source_id
        ).id
    return db, source_id, widget_id


def test_detached_snapshot_and_fresh_resolution(web_target):
    db, source_id, widget_id = web_target
    connections = []
    event.listen(db.engine, "checkout", lambda *args: connections.append("open"))
    event.listen(db.engine, "checkin", lambda *args: connections.append("closed"))
    target = resolve_web_target(db, widget_id)
    assert connections == ["open", "closed"]
    assert target.source_id == source_id
    assert target.url == "http://host/?share=private"
    assert "private" not in repr(target)
    with pytest.raises(FrozenInstanceError):
        target.url = "http://other"
    with db.transaction(write=True) as session:
        d.update_source(
            session,
            source_id,
            name="Web",
            kind="web_url",
            enabled=True,
            configuration={"url": "http://changed"},
        )
    assert resolve_web_target(db, widget_id).url == "http://changed"
    assert target.url == "http://host/?share=private"


@pytest.mark.parametrize(
    "problem",
    [
        "missing_widget",
        "disabled_widget",
        "widget_kind",
        "widget_config",
        "no_source",
        "disabled_source",
        "source_kind",
        "source_config",
    ],
)
def test_ineligible_target(web_target, problem):
    db, source_id, widget_id = web_target
    with db.transaction() as session:
        widget = d.get_widget(session, widget_id)
        source = d.get_source(session, source_id)
        if problem == "missing_widget":
            widget_id += 100
        elif problem == "disabled_widget":
            widget.enabled = False
        elif problem == "widget_kind":
            widget.kind = "video"
        elif problem == "widget_config":
            widget.configuration = {"duration": 10}
        elif problem == "no_source":
            widget.source_id = None
        elif problem == "disabled_source":
            source.enabled = False
        elif problem == "source_kind":
            source.kind = "local_directory"
        else:
            source.configuration = {"url": "file:///private"}
    with pytest.raises(WebSelectionError):
        resolve_web_target(db, widget_id)


def test_missing_referenced_source_is_not_selectable(web_target):
    db, source_id, widget_id = web_target
    # Simulate externally damaged state; ordinary domain writes enforce the FK.
    with sqlite3.connect(db.path) as connection:
        connection.execute("DELETE FROM source WHERE id = ?", (source_id,))
    with pytest.raises(WebSelectionError, match="Source is missing or disabled"):
        resolve_web_target(db, widget_id)
