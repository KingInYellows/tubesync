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
'''
import hmac
import ipaddress

from . import config


def client_ip(request):
    real_ip = request.META.get('HTTP_X_REAL_IP', '').strip()
    if real_ip:
        return real_ip
    return (request.META.get('REMOTE_ADDR') or '').strip()


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
