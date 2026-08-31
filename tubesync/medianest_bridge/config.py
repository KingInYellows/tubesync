'''
    Fork-local configuration for the medianest_bridge app.

    Every value here is read fresh from the environment (or the token file)
    on each call rather than cached at import time. This is a deliberate
    choice, not an oversight: it lets an operator rotate
    MEDIANEST_BRIDGE_TOKEN_FILE's contents, or flip MEDIANEST_BRIDGE_READ_ONLY,
    without a process restart, and it keeps tests free of module-level state
    that would otherwise need explicit resetting between cases. The bridge is
    a low-QPS diagnostics/control surface (not a hot media-serving path), so
    the extra file/env read per request is not a meaningful cost.

    None of these settings are registered in Django's settings.py — the app
    is deliberately self-configuring so that the fork's upstream-touch list
    stays limited to INSTALLED_APPS, the URL include, and the
    BASICAUTH_PREFIX_ALLOW_URIS exemption.
'''
import ipaddress
from pathlib import Path

from common.utils import getenv


# Contract version this app implements. Matches
# medianest_bridge/contract/bridge-openapi.v1.yaml's info.version.
BRIDGE_VERSION = '1.0.0'


def get_token():
    '''
        Returns the configured bearer token, or None if the bridge has not
        been configured with a token file (or the file is unreadable/empty).
        A None return is the fail-closed signal used by bridge_enabled().
    '''
    token_file = getenv('MEDIANEST_BRIDGE_TOKEN_FILE', '').strip()
    if not token_file:
        return None
    try:
        raw = Path(token_file).read_text(encoding='utf-8')
    except OSError:
        return None
    token = raw.strip()
    return token or None


def bridge_enabled():
    return get_token() is not None


def is_read_only():
    '''
        Defaults to True (read-only) unless explicitly set to "false".
        Any unset, malformed, or unrecognised value fails closed to
        read-only, matching MEDIANEST_BRIDGE_READ_ONLY's documented default.
    '''
    return 'false' != getenv('MEDIANEST_BRIDGE_READ_ONLY', 'true').strip().lower()


def max_body_bytes():
    return getenv('MEDIANEST_BRIDGE_MAX_BODY_BYTES', 65536, integer=True)


def allowed_cidrs():
    '''
        Returns a list of ip_network objects, or None if
        MEDIANEST_BRIDGE_ALLOWED_CIDRS is unset/empty (CIDR gate disabled;
        the bearer token remains the only access control in that case).
    '''
    raw = getenv('MEDIANEST_BRIDGE_ALLOWED_CIDRS', '').strip()
    if not raw:
        return None
    networks = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            # A malformed entry in an otherwise-configured allowlist must
            # not silently widen access. Fail closed: an unparsable
            # allowlist admits nothing.
            return []
    return networks


def upstream_sha():
    '''
        Full git SHA of the upstream commit this fork tracks, injected at
        image build time. "unknown" (not a fabricated value) when the
        build did not set it, e.g. local/CI test runs that never build the
        image (see /meta's contract description).
    '''
    return getenv('MEDIANEST_BRIDGE_UPSTREAM_SHA', 'unknown').strip() or 'unknown'
