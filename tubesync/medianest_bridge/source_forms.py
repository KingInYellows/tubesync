'''
    Builds a TubeSync SourceForm-compatible data dict from a bridge
    CreateSourceRequest payload, filling every field the bridge's request
    schema does not supply with TubeSync's own Source model defaults --
    via model_to_dict() on a blank instance, never a bridge-invented
    default -- then runs SourceForm's own validation plus
    EditSourceMixin's extra checks (media-format-produces-a-filename,
    directory-traversal via safe_join).

    See this app's T3 PR description for two documented contract
    resolutions this module implements:

    1. sourceType "channel" maps to TubeSync's CHANNEL_ID ('i') source
       type, never CHANNEL ('c'). TubeSync's real source_type has three
       values (channel-by-handle 'c', channel-by-durable-ID 'i',
       playlist 'p'); the contract's sourceType enum only distinguishes
       channel/playlist. CreateSourceRequest.canonicalKey's own
       description ("derived deterministically and injectively") implies
       a stable, durable identifier -- that matches 'i''s key format (a
       literal YouTube channel ID, URL https://www.youtube.com/channel/{key})
       not 'c''s (a /c/{name} handle, legacy and unstable). Consequence:
       MediaNest must supply a real YouTube channel ID as canonicalKey for
       channel sources, not an @handle string. ENDORSED by the contract
       owner: slice-1 MediaNest accepts channel submissions only as
       /channel/UC... URLs (or playlists) -- @handle and /c/name forms
       are rejected on the MediaNest side with SOURCE_INVALID guidance,
       since deterministic no-network derivation of a channel ID from a
       handle is impossible. canonicalKey arriving at POST /sources is
       therefore always a real channel ID or playlist ID, never a handle
       this app would need to resolve itself.

    2. ValidateSourceRequest has no `directory` field, and (per DECISIONS
       #27 on the canonical contract, confirming this app's own
       resolution) /sources/validate does not run the directory-traversal/
       media-format checks -- those need form.cleaned_data['directory'],
       which a full SourceForm cannot even produce without a `directory`
       value in the first place (Source.directory has no default,
       blank=False, so the FORM field is required -- submitting '' fails
       "this field is required" the same as omitting it entirely).
       validate_source_type_and_key() below validates just those two
       fields directly via their own Field.clean(), deliberately not
       attempting to build a whole SourceForm for the directory-less
       validate case -- see its own docstring for why a synthetic
       placeholder directory/name is not an acceptable substitute either.

    3. (T3) build_source_form() also accepts an optional
       `defaults_overlay` -- config.source_defaults()'s per-type
       operator-configured field overrides (MEDIANEST_BRIDGE_SOURCE_DEFAULTS),
       applied onto default_form_data() before the request's own
       type/key/name/directory. build_synthetic_source_form() and
       extract_form_errors() below exist so config.validate_source_defaults()
       can run that same overlay through this module's own validation
       path (for the `sourceDefaults` readiness component and POST
       /sources' own pre-check) without a real request or a saved row --
       see build_synthetic_source_form()'s own docstring for why a
       synthetic placeholder is acceptable there when it was rejected for
       /sources/validate above.
'''
import uuid

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.forms.models import model_to_dict
from django.utils._os import safe_join

from sync.choices import Val, YouTube_SourceType, youtube_validation_urls
from sync.forms import SourceForm
from sync.models import Source
from sync.utils import validate_url

CONTRACT_SOURCE_TYPES = ('channel', 'playlist')

_CONTRACT_TO_TUBESYNC_SOURCE_TYPE = {
    'channel': Val(YouTube_SourceType.CHANNEL_ID),
    'playlist': Val(YouTube_SourceType.PLAYLIST),
}

_ERRORS = {
    'invalid_media_format': (
        'Invalid media format, the media format contains errors or is empty.'
    ),
    'dir_outside_dlroot': 'Directory must be within the configured download root.',
}

# Fields whose model-level stored representation is not directly
# form-data-shaped. Currently just sponsorblock_categories: it is a
# CommaSepChoiceField (sync/fields.py) storing a comma-joined string
# ('all'), but its auto-generated form field is forms.MultipleChoiceField,
# which requires a list in `data` ('Enter a list of values.' otherwise --
# confirmed empirically, not guessed). No other SourceForm field has this
# mismatch as of this writing; if a future upstream field gains a similar
# custom field/widget pairing, add it here rather than special-casing it
# ad hoc at each call site.
_LIST_SHAPED_FIELDS = {'sponsorblock_categories'}

# T3: fields a MEDIANEST_BRIDGE_SOURCE_DEFAULTS overlay may never set --
# these are the four fields the create contract itself always supplies
# (CreateSourceRequest's sourceType/canonicalKey/name/directory, mapped by
# build_source_form() below) and that config.source_defaults() rejects
# outright if named in an overlay.
SOURCE_DEFAULTS_FORBIDDEN_FIELDS = frozenset({'source_type', 'key', 'name', 'directory'})


def allowed_source_default_fields():
    '''
        Every SourceForm field a MEDIANEST_BRIDGE_SOURCE_DEFAULTS overlay
        may set: SourceForm.base_fields minus the four the create contract
        owns (SOURCE_DEFAULTS_FORBIDDEN_FIELDS). Kept here rather than in
        config.py because SourceForm's field set is this module's own
        concern (default_form_data() already reads it the same way).
    '''
    return frozenset(SourceForm.base_fields.keys()) - SOURCE_DEFAULTS_FORBIDDEN_FIELDS


def contract_source_type_to_tubesync(contract_source_type):
    return _CONTRACT_TO_TUBESYNC_SOURCE_TYPE[contract_source_type]


def validate_canonical_url(contract_source_type, canonical_key, canonical_url):
    '''
        Validates canonicalUrl shape for the declared sourceType and
        requires the URL's extracted key to equal canonicalKey. Used by
        both POST /sources/validate and POST /sources.
    '''
    errors = []
    tubesync_source_type = contract_source_type_to_tubesync(contract_source_type)
    validator = youtube_validation_urls.get(tubesync_source_type)
    try:
        extracted_key = validate_url(canonical_url, validator)
    except ValidationError as exc:
        errors.append(
            f'canonicalUrl does not match sourceType {contract_source_type!r}: {exc}',
        )
        return errors
    if extracted_key != canonical_key:
        errors.append(
            f'canonicalUrl identifies key {extracted_key!r} but '
            f'canonicalKey is {canonical_key!r}',
        )
    return errors


def _coerce_list_shaped_fields(data):
    '''
        Mutates and returns `data` in place: normalizes any
        _LIST_SHAPED_FIELDS value to the list shape the auto-generated
        form field expects (see _LIST_SHAPED_FIELDS' own comment). Shared
        by default_form_data() (TubeSync's own model default is the
        comma-joined string form) and build_source_form()/
        build_synthetic_source_form() (a MEDIANEST_BRIDGE_SOURCE_DEFAULTS
        overlay could supply either shape -- a plain string like the model
        default, or already a list).
    '''
    for field in _LIST_SHAPED_FIELDS:
        if field in data and not isinstance(data[field], list):
            data[field] = [data[field]] if data[field] else []
    return data


def default_form_data():
    '''TubeSync's own Source model defaults for every SourceForm field.'''
    blank = Source()
    data = model_to_dict(blank, fields=list(SourceForm.base_fields.keys()))
    return _coerce_list_shaped_fields(data)


def validate_source_type_and_key(*, source_type, key):
    '''
        Field-level-only validation for source_type/key, used by
        /sources/validate. Runs each field's own validators directly
        (choices, max_length, blank) via Field.clean() -- no DB query, no
        uniqueness check. That absence is deliberate, not an oversight:
        validate must not reject a key that legitimately already exists
        (adopt-on-conflict is POST /sources's job, via 409
        SOURCE_CONFLICT, not validate's).

        A synthetic placeholder name/directory (to let a full SourceForm
        run instead) was considered and rejected: it could collide with a
        real existing row's name/directory by chance, failing validate
        for a reason that has nothing to do with the caller's actual
        request, and the directory-traversal check that placeholder would
        feed into is meaningless against a value never actually used.

        Returns a list of error strings, empty if valid.
    '''
    errors = []
    for field_name, value in (('source_type', source_type), ('key', key)):
        field = SourceForm.base_fields[field_name]
        try:
            field.clean(value)
        except ValidationError as exc:
            errors.append(f'{field_name}: {"; ".join(exc.messages)}')
    return errors


def build_source_form(*, source_type, key, name, directory, defaults_overlay=None):
    '''
        Returns a SourceForm with is_valid() already evaluated (so
        .errors/.cleaned_data are populated either way). Used by
        POST /sources only -- CreateSourceRequest always supplies name
        and directory, so this always builds a complete, real form; there
        is no directory-less variant of this function (see
        validate_source_type_and_key() for that case).

        `defaults_overlay` (T3): config.source_defaults()'s per-type dict
        of SourceForm field overrides, applied onto default_form_data()
        BEFORE source_type/key/name/directory below -- so an overlay can
        never override what the request itself supplies, even if it
        somehow named one of those keys (config.source_defaults() already
        rejects that at parse time; this ordering is a second,
        structural guarantee of the same thing). Only bridge-created
        sources go through this overlay -- the HTML UI builds its own
        SourceForm directly in sync/views/sources.py, untouched by this
        module.
    '''
    data = default_form_data()
    if defaults_overlay:
        data.update(defaults_overlay)
        _coerce_list_shaped_fields(data)
    data['source_type'] = source_type
    data['key'] = key
    data['name'] = name
    data['directory'] = directory
    form = SourceForm(data=data)
    form.is_valid()
    return form


def build_synthetic_source_form(*, contract_source_type, overlay):
    '''
        Builds a real SourceForm from default_form_data() overlaid with
        `overlay` (already validated against
        allowed_source_default_fields() by config.source_defaults() --
        this function does not re-check that), plus a synthetic-but-safe
        key/name/directory that is NEVER saved and used for nothing
        beyond satisfying SourceForm's own required-field/clean()
        machinery. Used only by config.validate_source_defaults() to run
        a MEDIANEST_BRIDGE_SOURCE_DEFAULTS overlay through the exact same
        field-level checks (is_valid()) plus run_edit_source_checks()
        (media-format-produces-a-filename, directory-traversal) that a
        real POST /sources create applies via build_source_form() above
        -- so a broken overlay is caught by readiness/create-time
        validation instead of only failing every subsequent real create
        one at a time.

        Why a synthetic key/name/directory is acceptable HERE when
        validate_source_type_and_key()'s own docstring explicitly
        rejected the same idea for POST /sources/validate: that rejection
        was about a caller-supplied *request*, where a placeholder value
        could fail (or wrongly succeed) for a reason that has nothing to
        do with what the caller actually asked to validate. Here there is
        no request being validated -- only server-side configuration that
        applies identically to every future create of that source type --
        so a fixed synthetic placeholder cannot mask or misrepresent a
        caller's own input; there is none.

        Uniqueness is irrelevant to "is this configuration well-formed"
        and must not depend on what happens to already exist in the
        database (a real key/name/directory collision here would make
        configuration validity flap based on unrelated data). Rather than
        relying on the synthetic values below being merely unlikely to
        collide, validate_unique() is overridden to a no-op explicitly.
    '''
    data = default_form_data()
    data.update(overlay)
    _coerce_list_shaped_fields(data)
    data['source_type'] = contract_source_type_to_tubesync(contract_source_type)
    placeholder = f'medianest-bridge-config-check-{uuid.uuid4().hex}'
    data['key'] = placeholder
    data['name'] = placeholder
    data['directory'] = placeholder
    form = SourceForm(data=data)
    form.validate_unique = lambda: None
    form.is_valid()
    return form


def extract_form_errors(form):
    '''
        Plain-text "field: message" strings from form.errors, via
        Django's own ErrorDict.get_json_data() -- str(form.errors) would
        render as Django's own HTML (`<ul class="errorlist">...</ul>`),
        which a JSON/API consumer must never receive (T4 verifier MEDIUM
        finding, reproduced live in a POST /sources response). Shared by
        views_write.py (a real create's errors) and
        config.validate_source_defaults() (a synthetic overlay-check
        form's errors), so both flow through one implementation.
    '''
    messages = []
    for field, field_errors in form.errors.get_json_data().items():
        for error in field_errors:
            messages.append(f'{field}: {error["message"]}')
    return messages


def run_edit_source_checks(form):
    '''
        Reproduces EditSourceMixin.form_valid()'s two extra checks
        (sync/views/sources.py), which are not expressible as ordinary
        Django form field validators. Mutates form.errors in place,
        matching EditSourceMixin's own pattern; callers should re-check
        form.is_valid() afterward.
    '''
    if 'directory' not in form.cleaned_data or 'media_format' not in form.cleaned_data:
        return
    obj = form.save(commit=False)
    saved_media_format = obj.media_format
    obj.media_format = form.cleaned_data['media_format']
    example_media_file = obj.get_example_media_format()
    obj.media_format = saved_media_format
    if '' == example_media_file:
        form.add_error(
            'media_format', ValidationError(_ERRORS['invalid_media_format']),
        )
    try:
        target_check = form.cleaned_data['directory'] + '/.virt'
        safe_join(settings.DOWNLOAD_ROOT, target_check)
    except SuspiciousFileOperation:
        # T4 verifier MEDIUM: this message previously embedded
        # settings.DOWNLOAD_ROOT verbatim (f' ({settings.DOWNLOAD_ROOT})')
        # -- reproduced live by the verifier as a real leak of the
        # server's own filesystem layout. "Must be within the configured
        # download root" is honest and actionable without naming the
        # actual path; the caller doesn't need the literal value to
        # correct their request.
        form.add_error('directory', ValidationError(_ERRORS['dir_outside_dlroot']))
