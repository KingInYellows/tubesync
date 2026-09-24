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
import json
from pathlib import Path

from common.utils import getenv


# Contract version this app implements. Matches
# medianest_bridge/contract/bridge-openapi.v1.yaml's info.version.
BRIDGE_VERSION = '1.0.0'

# T3: the two contract source types this profile can be keyed by, plus the
# optional shared "*" block MEDIANEST_BRIDGE_SOURCE_DEFAULTS may use.
_SOURCE_DEFAULTS_TYPES = ('channel', 'playlist')

# Built-in profile applied when MEDIANEST_BRIDGE_SOURCE_DEFAULTS is unset
# (distinct from an explicit `{}`, which means "no overrides" -- see
# source_defaults()'s docstring). Per the T3 plan's Answers section: a
# playlist is treated as its own show using the same date-based Plex TV
# Shows scheme as a channel, so both types share this one profile. No
# Shorts filter by default (open question, left undecided -> none applied).
_BUILTIN_SOURCE_DEFAULTS_PROFILE = {
    'write_nfo': True,
    'copy_thumbnails': True,
    'copy_channel_images': True,
    'index_streams': False,
    'media_format': (
        'Season {episode_yyyy}/'
        's{episode_yyyy}e{episode_mmddnn} - {title_full_bounded} [{key}].{ext}'
    ),
}


class SourceDefaultsConfigError(Exception):
    '''
        Raised by source_defaults() for any invalid
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS shape or content: malformed JSON,
        a non-object value, an unknown top-level key, a non-object
        per-type/`*` block, or a per-type block that names a field
        POST /sources' contract already owns (sourceType/canonicalKey/
        name/directory) or that SourceForm does not have at all.

        str(exc) is safe to surface directly (after
        errors.error_response()'s own sanitize_error_message() pass, for
        the POST /sources 503 path, or as-is in the sourceDefaults
        readiness component's `detail`): every message here names the
        failing key/field, never the env var's own raw value.
    '''


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


def source_defaults():
    '''
        Returns {'channel': {...}, 'playlist': {...}}, each value a dict of
        SourceForm field overrides to overlay onto default_form_data() for
        a bridge-created source of that type (source_forms.build_source_form()
        is the only caller that applies these -- the HTML UI's own source
        forms are untouched). Read fresh from
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS on every call, matching every
        other function in this module.

        - Unset/empty env var: returns the built-in profile
          (_BUILTIN_SOURCE_DEFAULTS_PROFILE) for both types.
        - `{}` (an explicitly empty top-level JSON object): the documented
          all-types escape hatch -- returns {'channel': {}, 'playlist':
          {}}, i.e. no overrides at all, plain SourceForm/model defaults
          for both types.
        - Otherwise: a JSON object whose keys are `channel`, `playlist`,
          and/or the shared `*` block (any other top-level key is a
          configuration error). Every contract source type MUST be
          covered by the object: either its own key is present (any
          value, including an explicit `{}`), or a `*` key is present
          (which covers both types at once, present or not). A type
          covered by NEITHER is a configuration error, not a silent
          "no overrides" -- an operator who configures only `channel`
          must not have `playlist` sources silently created with no
          overrides and no signal that anything is different; that is
          exactly the silent-failure shape this function refuses to
          produce. Precedence when a type IS covered:
            * An explicit per-type `{}` (the key is present with an empty
              object) is a full per-type opt-out -- that type gets NO
              overrides at all, deliberately ignoring `*` too, the same
              way the top-level `{}` escape hatch ignores everything else.
              This is the one way to explicitly exempt a single type
              while still configuring the other.
            * Otherwise the type's own overlay (or {} if only covered via
              `*`) is merged onto `*` (`*` first, the per-type overlay's
              own fields winning on conflict).
          There is no way to opt one type back into the BUILT-IN profile
          while customizing only the other through this variable -- the
          built-in profile only applies when the whole env var is unset;
          set both explicitly (or use `*`) once any customization is in
          play.

        Every resulting per-type block (other than a `{}` full opt-out,
        which has nothing to check) is validated against the same field
        allowlist POST /sources's own create path is built from
        (source_forms.allowed_source_default_fields(): every SourceForm
        field except source_type/key/name/directory, which the create
        contract itself owns) -- an unknown or forbidden field raises
        SourceDefaultsConfigError rather than being silently dropped or
        silently applied.

        Raises SourceDefaultsConfigError for anything else: malformed
        JSON, a non-object top-level value, an unknown top-level key, an
        uncovered source type, or a non-object per-type/`*` value. This
        function does NOT run SourceForm's own field-level validation
        (does the resulting media_format actually produce a filename, is
        the directory safe, etc.) -- that is validate_source_defaults()'s
        job, one layer up, via a real (never-saved) SourceForm.
    '''
    # Deferred import: avoids importing sync.forms (which touches Django's
    # app registry) at config.py's own module-import time -- config.py is
    # read on every request, including before the app registry may be
    # fully ready in some import orders (see readiness.py's own deferred
    # Django imports for the same reasoning).
    from .source_forms import allowed_source_default_fields

    raw = getenv('MEDIANEST_BRIDGE_SOURCE_DEFAULTS', '').strip()
    if not raw:
        return {
            'channel': dict(_BUILTIN_SOURCE_DEFAULTS_PROFILE),
            'playlist': dict(_BUILTIN_SOURCE_DEFAULTS_PROFILE),
        }
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceDefaultsConfigError(
            f'MEDIANEST_BRIDGE_SOURCE_DEFAULTS is not valid JSON: {exc}',
        ) from exc
    if not isinstance(parsed, dict):
        raise SourceDefaultsConfigError(
            'MEDIANEST_BRIDGE_SOURCE_DEFAULTS must be a JSON object.',
        )
    if not parsed:
        # Explicit escape hatch -- see docstring.
        return {'channel': {}, 'playlist': {}}

    allowed_top_level = set(_SOURCE_DEFAULTS_TYPES) | {'*'}
    unknown_top_level = set(parsed.keys()) - allowed_top_level
    if unknown_top_level:
        raise SourceDefaultsConfigError(
            'MEDIANEST_BRIDGE_SOURCE_DEFAULTS has unknown top-level '
            f'key(s): {sorted(unknown_top_level)!r}.',
        )

    has_star = '*' in parsed
    shared = parsed.get('*', {})
    if not isinstance(shared, dict):
        raise SourceDefaultsConfigError(
            'MEDIANEST_BRIDGE_SOURCE_DEFAULTS["*"] must be a JSON object.',
        )

    allowed_fields = allowed_source_default_fields()
    result = {}
    for source_type in _SOURCE_DEFAULTS_TYPES:
        type_present = source_type in parsed
        if not type_present and not has_star:
            # Silent-failure guard -- see docstring. A type covered by
            # neither its own key nor "*" must fail loudly, not fall back
            # to "no overrides" without telling the operator anything
            # changed for that type.
            raise SourceDefaultsConfigError(
                'MEDIANEST_BRIDGE_SOURCE_DEFAULTS does not cover source '
                f'type {source_type!r}: add a {source_type!r} key (an '
                'empty {} is a valid explicit opt-out for just that '
                'type) or a shared "*" block that covers both types. Use '
                'a top-level {} to opt every type out at once.',
            )

        overlay = parsed.get(source_type, {})
        if not isinstance(overlay, dict):
            raise SourceDefaultsConfigError(
                f'MEDIANEST_BRIDGE_SOURCE_DEFAULTS[{source_type!r}] must '
                'be a JSON object.',
            )

        if type_present and not overlay:
            # Explicit per-type {}: a full opt-out for this type alone,
            # deliberately bypassing "*" too -- see docstring. Distinct
            # from `overlay` merely defaulting to {} because the type was
            # only covered via "*" (type_present is False in that case),
            # which DOES still inherit "*"'s fields below.
            result[source_type] = {}
            continue

        merged = {**shared, **overlay}
        unknown_fields = set(merged.keys()) - allowed_fields
        if unknown_fields:
            raise SourceDefaultsConfigError(
                f'MEDIANEST_BRIDGE_SOURCE_DEFAULTS[{source_type!r}] has '
                f'unknown or forbidden field(s): {sorted(unknown_fields)!r}.',
            )
        result[source_type] = merged
    return result


def validate_source_defaults():
    '''
        Runs MEDIANEST_BRIDGE_SOURCE_DEFAULTS through the same path
        source_defaults() plus a real create's field-level SourceForm
        checks and run_edit_source_checks() (media-format-produces-a-
        filename, directory-traversal) would apply, for both source
        types, so a broken configuration surfaces once here -- the
        `sourceDefaults` readiness component, and POST /sources' own
        pre-check below -- rather than only as every subsequent create
        failing one at a time with no diagnosis.

        Returns a list of plain-text error strings (empty = valid). Each
        string is safe to surface directly: source_defaults() and
        source_forms.extract_form_errors() only ever name failing
        keys/fields, never echo the env var's own raw value.

        An explicitly empty overlay for a type ({} -- from the top-level
        escape hatch, an explicit per-type {} opt-out, or a type covered
        only by an empty "*" block) is not run through SourceForm at all:
        default_form_data() alone is already known-valid (it's TubeSync's
        own Source model defaults), so there's nothing to check.
    '''
    from .source_forms import build_synthetic_source_form, extract_form_errors, run_edit_source_checks

    try:
        defaults_by_type = source_defaults()
    except SourceDefaultsConfigError as exc:
        return [str(exc)]

    errors = []
    for source_type, overlay in defaults_by_type.items():
        if not overlay:
            continue
        form = build_synthetic_source_form(contract_source_type=source_type, overlay=overlay)
        if not form.is_valid():
            errors.extend(f'{source_type}: {message}' for message in extract_form_errors(form))
            continue
        # Only safe to call once the form is already valid -- see
        # CreateSourceView.post's identical guard in views_write.py for
        # why (form.save(commit=False) raises unconditionally otherwise).
        run_edit_source_checks(form)
        if not form.is_valid():
            errors.extend(f'{source_type}: {message}' for message in extract_form_errors(form))
    return errors
