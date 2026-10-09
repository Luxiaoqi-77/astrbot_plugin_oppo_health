"""Small, framework-independent checks for the care page's server-side access."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit


def dashboard_admin_matches(config: object, username: object) -> bool:
    if not isinstance(config, Mapping):
        return False
    dashboard = config.get("dashboard")
    admin = dashboard.get("username") if isinstance(dashboard, Mapping) else None
    return bool(
        isinstance(admin, str) and admin.strip()
        and isinstance(username, str) and username == admin
    )


def same_origin_write(origin: object, host: object, fetch_site: object = None) -> bool:
    """Require an exact browser Origin/Host match and same-origin fetch metadata."""
    if not isinstance(origin, str) or not isinstance(host, str) or not host.strip():
        return False
    if fetch_site != "same-origin":
        return False
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return False
        # Accessing .port also validates malformed ports; retain the original
        # authority so explicit ports must match the Host header exactly.
        _ = parsed.port
    except ValueError:
        return False
    return parsed.netloc.casefold() == host.strip().casefold()
