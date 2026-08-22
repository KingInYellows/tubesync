'''
    Strict, hand-written validation for the two POST write request bodies
    (ValidateSourceRequest, CreateSourceRequest), matching the vendored
    contract's schemas field-for-field: unknown top-level keys are
    rejected, required fields must be present, string fields are bounded
    to the contract's declared minLength/maxLength, sourceType must be one
    of the contract's enum values. No JSON-Schema library is introduced --
    the request shapes are small and fixed, so an explicit per-field check
    is more auditable than a generic schema engine for two request types.

    Deliberately validates SourceProfile's structure (type-check each
    field, reject unknown profile keys) even though source_forms.py does
    not currently map profile.* onto any TubeSync Source field for T3
    (per this app's T3 PR description: created sources use TubeSync's own
    model defaults, not profile-derived values, pending a contract-level
    specification of the exact field-name/enum-value mapping) -- accepting
    and structurally validating profile now, without acting on it, keeps
    the contract's request shape honored without silently guessing a
    mapping that could be wrong.
'''
from .source_forms import CONTRACT_SOURCE_TYPES

_PROFILE_FIELDS = {
    'resolution': str,
    'videoCodec': str,
    'audioCodec': str,
    'downloadCap': str,
    'subtitles': list,
    'autoSubtitles': bool,
    'sponsorBlock': bool,
    'deleteOldMedia': bool,
    'deleteRemovedMedia': bool,
    'deleteFilesOnDisk': bool,
}

_VALIDATE_REQUIRED = ('sourceType', 'canonicalKey', 'canonicalUrl')
_CREATE_REQUIRED = ('sourceType', 'canonicalKey', 'canonicalUrl', 'name', 'directory')

_ALLOWED_KEYS_VALIDATE = frozenset(_VALIDATE_REQUIRED) | {'profile'}
_ALLOWED_KEYS_CREATE = frozenset(_CREATE_REQUIRED) | {'profile'}


def _errors_for_common_fields(body):
    errors = []
    if 'sourceType' in body and body['sourceType'] not in CONTRACT_SOURCE_TYPES:
        errors.append(f'sourceType must be one of {list(CONTRACT_SOURCE_TYPES)!r}.')
    if 'canonicalKey' in body:
        key = body['canonicalKey']
        if not isinstance(key, str) or not (1 <= len(key) <= 100):
            errors.append('canonicalKey must be a string of 1-100 characters.')
    if 'canonicalUrl' in body:
        url = body['canonicalUrl']
        if not isinstance(url, str) or not (1 <= len(url) <= 2048):
            errors.append('canonicalUrl must be a string of 1-2048 characters.')
    if 'profile' in body and body['profile'] is not None:
        errors.extend(_errors_for_profile(body['profile']))
    return errors


def _errors_for_profile(profile):
    errors = []
    if not isinstance(profile, dict):
        return ['profile must be an object.']
    unknown = set(profile.keys()) - set(_PROFILE_FIELDS.keys())
    if unknown:
        errors.append(f'profile has unknown field(s): {sorted(unknown)!r}.')
    for field, expected_type in _PROFILE_FIELDS.items():
        if field not in profile:
            continue
        value = profile[field]
        if expected_type is bool:
            if not isinstance(value, bool):
                errors.append(f'profile.{field} must be a boolean.')
        elif expected_type is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f'profile.{field} must be an array of strings.')
        elif not isinstance(value, str):
            errors.append(f'profile.{field} must be a string.')
    return errors


def validate_validate_source_request(body):
    if not isinstance(body, dict):
        return ['Request body must be a JSON object.']
    errors = []
    unknown = set(body.keys()) - _ALLOWED_KEYS_VALIDATE
    if unknown:
        errors.append(f'Request has unknown field(s): {sorted(unknown)!r}.')
    for field in _VALIDATE_REQUIRED:
        if field not in body:
            errors.append(f'{field} is required.')
    errors.extend(_errors_for_common_fields(body))
    return errors


def validate_create_source_request(body):
    if not isinstance(body, dict):
        return ['Request body must be a JSON object.']
    errors = []
    unknown = set(body.keys()) - _ALLOWED_KEYS_CREATE
    if unknown:
        errors.append(f'Request has unknown field(s): {sorted(unknown)!r}.')
    for field in _CREATE_REQUIRED:
        if field not in body:
            errors.append(f'{field} is required.')
    errors.extend(_errors_for_common_fields(body))
    for field in ('name', 'directory'):
        if field in body:
            value = body[field]
            if not isinstance(value, str) or not (1 <= len(value) <= 100):
                errors.append(f'{field} must be a string of 1-100 characters.')
    return errors
