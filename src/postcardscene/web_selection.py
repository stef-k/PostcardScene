"""Web configuration semantics and DB-only presentation snapshots."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from postcardscene.domain import Source, Widget


class WebSelectionError(ValueError):
    """Invalid web configuration or a non-selectable Widget/Source."""


def validate_web_source(source_kind, configuration):
    """Return normalized configuration without DNS, network or browser work."""
    if source_kind != "web_url":
        raise WebSelectionError("Source must be web_url.")
    if type(configuration) is not dict or set(configuration) != {"url"}:
        raise WebSelectionError("Web Source configuration must contain only url.")
    value = configuration["url"]
    if not isinstance(value, str):
        raise WebSelectionError("URL must be a string.")
    url = value.strip()
    if not 0 < len(url) <= 8192 or any(
        ord(char) < 32 or ord(char) == 127 or char.isspace() or char == "\\"
        for char in url
    ):
        raise WebSelectionError(
            "URL must be at most 8192 characters without whitespace, controls or backslashes."
        )
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or "@" in parsed.netloc
            or parsed.port == 0
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        # urlsplit is a parser, not a hostname/authority validator.
        if any(char in parsed.hostname for char in '<>"^`{|}%'):
            raise ValueError
        if "[" in parsed.netloc and not re.fullmatch(
            r"\[[^\[\]]+\](?::[0-9]+)?", parsed.netloc
        ):
            raise ValueError
    except ValueError:
        raise WebSelectionError(
            "Enter an absolute HTTP/HTTPS URL with a hostname and valid port, without userinfo."
        ) from None
    return {"url": url}


def validate_web_configuration(widget_kind, configuration):
    if widget_kind != "web_view" or type(configuration) is not dict or configuration:
        raise WebSelectionError("Web Widget must be web_view with empty configuration.")
    return {}


@dataclass(frozen=True)
class WebTarget:
    """Presentation input only; resolve current configuration for each new step."""

    source_id: int
    url: str = field(repr=False)


def resolve_web_target(database, widget_id):
    """Finish one short transaction before returning detached renderer input."""
    with database.transaction() as session:
        widget = session.get(Widget, widget_id)
        if widget is None or not widget.enabled:
            raise WebSelectionError("Web Widget is missing or disabled.")
        validate_web_configuration(widget.kind, widget.configuration)
        source = session.get(Source, widget.source_id) if widget.source_id else None
        if source is None or not source.enabled:
            raise WebSelectionError("Web Source is missing or disabled.")
        configuration = validate_web_source(source.kind, source.configuration)
        return WebTarget(source.id, configuration["url"])
