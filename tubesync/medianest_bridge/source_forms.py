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
       channel sources, not an @handle string.

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
'''
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.forms.models import model_to_dict
from django.utils._os import safe_join

from sync.choices import Val, YouTube_SourceType
from sync.forms import SourceForm
from sync.models import Source

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


def contract_source_type_to_tubesync(contract_source_type):
    return _CONTRACT_TO_TUBESYNC_SOURCE_TYPE[contract_source_type]


def default_form_data():
    '''TubeSync's own Source model defaults for every SourceForm field.'''
    blank = Source()
    data = model_to_dict(blank, fields=list(SourceForm.base_fields.keys()))
    for field in _LIST_SHAPED_FIELDS:
        if field in data and not isinstance(data[field], list):
            data[field] = [data[field]] if data[field] else []
    return data


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


def build_source_form(*, source_type, key, name, directory):
    '''
        Returns a SourceForm with is_valid() already evaluated (so
        .errors/.cleaned_data are populated either way). Used by
        POST /sources only -- CreateSourceRequest always supplies name
        and directory, so this always builds a complete, real form; there
        is no directory-less variant of this function (see
        validate_source_type_and_key() for that case).
    '''
    data = default_form_data()
    data['source_type'] = source_type
    data['key'] = key
    data['name'] = name
    data['directory'] = directory
    form = SourceForm(data=data)
    form.is_valid()
    return form


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
        form.add_error(
            'directory',
            ValidationError(
                _ERRORS['dir_outside_dlroot'] + f' ({settings.DOWNLOAD_ROOT})',
            ),
        )
