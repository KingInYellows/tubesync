'''
    Bearer-token and CIDR access control for the bridge, plus the
    trustworthy client-IP resolver the CIDR gate depends on.

    Deliberately does NOT reuse common.utils.get_client_ip(): that helper
    takes the *first* comma-separated entry of a client-supplied
    X-Forwarded-For header, which is attacker-controlled (a client can
    prepend arbitrary IPs ahead of the real one). That is an acceptable
    trade-off for its one existing caller (HealthCheckView's
    HEALTHCHECK_ALLOWED_IPS, a defense-in-depth check on an unauthenticated
    endpoint), but ADR-0006 Sec.2/3 promotes MEDIANEST_BRIDGE_ALLOWED_CIDRS
    to *primary* access control across a real LAN network segment, not
    defense-in-depth -- reusing the spoofable helper here would silently
    defeat that.

    The trustworthy signal instead is X-Real-IP, which this fork's own
    openresty config (config/root/etc/nginx/nginx.conf) sets unconditionally
    from $remote_addr on every proxied request:

        proxy_set_header X-Real-IP $remote_addr;

    nginx OVERWRITES this header rather than passing through any
    client-supplied value, and gunicorn (tubesync/tubesync/gunicorn.py)
    binds to 127.0.0.1 by default -- i.e. it is only reachable through
    nginx's proxy_pass, not directly from the LAN. Under that topology
    X-Real-IP is the actual TCP peer address as seen by nginx and cannot be
    forged by the client. This trust chain assumes nginx remains the sole
    ingress in front of gunicorn; if that topology ever changes (e.g. a
    load balancer placed in front of nginx), X-Real-IP's trustworthiness
    would need re-establishing at that new hop.

    REMOTE_ADDR is used as a fallback for direct-to-Django access with no
    proxy in front (Django's runserver, the test client) where X-Real-IP is
    never set.

    That trust chain has a runtime gap, not just a topology assumption:
    nothing in this app *verifies* gunicorn is actually loopback-bound.
    gunicorn.py's own LISTEN_HOST default is 127.0.0.1, but an operator can
    override it via the LISTEN_HOST env var. If they point it at a
    non-loopback address, a LAN host can reach gunicorn directly, bypassing
    nginx entirely, and set X-Real-IP itself -- forging an allowlisted
    address and defeating the CIDR gate (the bearer token is still
    required, so this degrades the control rather than removing all auth).
    cidr_trust_warning() below detects that specific misconfiguration and
    logs a loud warning; it does not hard-fail, since an operator may have
    a legitimate alternate ingress this app has no way to verify.
'''
import hmac
import ipaddress
import os

from . import config

LOOPBACK_LISTEN_HOST_DEFAULT = '127.0.0.1'


def client_ip(request):
    real_ip = request.META.get('HTTP_X_REAL_IP', '').strip()
    if real_ip:
        return real_ip
    return (request.META.get('REMOTE_ADDR') or '').strip()


def _listen_host_is_loopback():
    '''
        Mirrors gunicorn.py's own LISTEN_HOST env var and default so this
        check reflects the same effective bind address gunicorn will
        actually use, without spawning or introspecting the gunicorn
        process itself.
    '''
    host = (os.environ.get('LISTEN_HOST') or '').strip() or LOOPBACK_LISTEN_HOST_DEFAULT
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not a literal IP we can classify (e.g. an arbitrary hostname).
        # Fail toward warning rather than silently assuming it's safe.
        return False


def cidr_trust_warning():
    '''
        Returns a warning string when MEDIANEST_BRIDGE_ALLOWED_CIDRS is
        configured but LISTEN_HOST is not loopback -- the specific
        misconfiguration that lets a LAN host bypass nginx, reach gunicorn
        directly, and forge X-Real-IP to impersonate an allowlisted
        address. Returns None when the condition doesn't hold (no CIDRs
        configured, or LISTEN_HOST is loopback). Never mentions the bearer
        token.
    '''
    if config.allowed_cidrs() is None:
        return None
    if _listen_host_is_loopback():
        return None
    return (
        'medianest_bridge: MEDIANEST_BRIDGE_ALLOWED_CIDRS is configured but '
        'LISTEN_HOST is not loopback. X-Real-IP is only trustworthy when '
        'gunicorn is reachable exclusively through the bundled nginx '
        '(LISTEN_HOST=127.0.0.1, the default) -- with gunicorn reachable '
        'directly, any host on that network can set X-Real-IP itself and '
        'impersonate an allowlisted address. CIDR enforcement is degraded '
        'to a no-op; the bearer token is still required and unaffected.'
    )


def cidr_allowed(request):
    '''
        True if MEDIANEST_BRIDGE_ALLOWED_CIDRS is unset (gate disabled) or
        the resolved client IP falls within one of the configured networks.
        False (deny) if the gate is configured but the client IP is missing,
        unparsable, or outside every configured network.
    '''
    networks = config.allowed_cidrs()
    if networks is None:
        return True
    ip_str = client_ip(request)
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def bearer_token_valid(request):
    '''
        Constant-time bearer token comparison via hmac.compare_digest. The
        token itself is never logged or included in any response -- callers
        must not log the raw Authorization header on failure.
    '''
    expected = config.get_token()
    if not expected:
        return False
    header = request.META.get('HTTP_AUTHORIZATION', '')
    prefix = 'Bearer '
    if not header.startswith(prefix):
        return False
    supplied = header[len(prefix):].strip()
    return hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8'))
