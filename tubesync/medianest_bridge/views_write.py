'''
    T3 write endpoints: POST /sources/validate, POST /sources,
    POST /sources/{sourceUuid}/sync. The first mutation surface -- gated
    by MEDIANEST_BRIDGE_READ_ONLY (default true) via the same
    BridgeView.dispatch() every T1/T2 route already uses; nothing here
    adds a separate read-only check.

    No deletion of any kind exists in this app, here or elsewhere.
'''
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _

from common.models import TaskHistory
from sync.choices import youtube_validation_urls
from sync.models import Source
from sync.tasks import index_source
from sync.utils import validate_url

from . import mapping
from .errors import error_response
from .request_schemas import validate_create_source_request, validate_validate_source_request
from .source_forms import (
    build_source_form,
    contract_source_type_to_tubesync,
    run_edit_source_checks,
    validate_source_type_and_key,
)
from .sync_dedup import find_pending_or_running_index_task
from .views import BridgeView
from .views_sources import SourceLookupView, _get_source_or_error


def _parse_json_body(request, request_id):
    '''Returns (body, None) or (None, error_response).'''
    try:
        raw = request.body.decode('utf-8')
    except UnicodeDecodeError:
        return None, error_response(
            status=400, code='SOURCE_INVALID', title='Invalid request body',
            detail='Request body is not valid UTF-8.',
            request_id=request_id, retryable=False,
        )
    if not raw.strip():
        return None, error_response(
            status=400, code='SOURCE_INVALID', title='Invalid request body',
            detail='Request body must not be empty.',
            request_id=request_id, retryable=False,
        )
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None, error_response(
            status=400, code='SOURCE_INVALID', title='Invalid request body',
            detail='Request body must be valid JSON.',
            request_id=request_id, retryable=False,
        )
    return body, None


def _invalid(request_id, errors):
    return error_response(
        status=400,
        code='SOURCE_INVALID',
        title='Invalid request',
        detail='; '.join(errors),
        request_id=request_id,
        retryable=False,
    )


class ValidateSourceView(BridgeView):
    '''
        POST /sources/validate -- validates a candidate source WITHOUT
        persisting anything. See source_forms.py's module docstring for
        the two documented gaps between the contract's prose description
        of this endpoint and its own request schema (no `directory`
        field, so the directory-traversal/media-format checks cannot
        run; `displayName` has no clean TubeSync-side source of truth
        without a live metadata fetch, so it echoes canonicalKey as an
        explicit placeholder).

        read_only_exempt = True: this endpoint never mutates anything,
        and the contract's own operation definition for it lists no 403
        ReadOnly response (unlike POST /sources and
        POST /sources/{uuid}/sync, which both do) -- see
        BridgeView.read_only_exempt's docstring in views.py.
    '''

    read_only_exempt = True

    def post(self, request, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_validate_source_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)

        contract_source_type = body['sourceType']
        canonical_key = body['canonicalKey']
        canonical_url = body['canonicalUrl']
        tubesync_source_type = contract_source_type_to_tubesync(contract_source_type)

        validator = youtube_validation_urls.get(tubesync_source_type)
        try:
            validate_url(canonical_url, validator)
        except ValidationError as exc:
            return _invalid(
                request_id,
                [f'canonicalUrl does not match sourceType {contract_source_type!r}: {exc}'],
            )

        field_errors = validate_source_type_and_key(
            source_type=tubesync_source_type, key=canonical_key,
        )
        if field_errors:
            return _invalid(request_id, field_errors)

        return _json_response({
            'sourceType': contract_source_type,
            'canonicalKey': canonical_key,
            'canonicalUrl': canonical_url,
            # No TubeSync-side display name exists without a live
            # metadata fetch (out of scope here) -- explicit placeholder,
            # not a fabricated title. See this class's docstring.
            'displayName': canonical_key,
            'thumbnailUrl': None,
        })


class CreateSourceView(SourceLookupView):
    '''
        GET /sources (inherited from SourceLookupView, T2's key-lookup
        endpoint -- the contract's /sources path item has both a get and
        a post operation, so both live on one view class registered at
        one URL) + POST /sources -- create-or-adopt on the canonical key.
        Ordering,
        per the contract: a `key` collision always wins over a
        `name`/`directory` collision (409 SOURCE_CONFLICT, adopt
        semantics) even if the request's name/directory also happen to
        collide with some other row.

        Concurrency: an initial SELECT-based pre-check narrows the common
        case, but the actual DB-level UNIQUE constraint on key/name/
        directory is the real convergence mechanism -- form.save() is
        wrapped in try/except IntegrityError, and on conflict this
        re-queries (get-after-conflict) rather than trusting the
        pre-check's now-stale result.

        Side effects: a successful form.save() goes through Source's real
        .save(), which fires sync/signals.py's post_save receiver exactly
        as a human using the HTML UI would trigger -- schedules
        check_source_directory_exists, conditionally download_source_images,
        TaskHistory.schedule(index_source, delay=600) if the source is
        active, and TaskHistory.schedule(save_all_media_for_source, ...).
        This is deliberate (ADR-0006 Sec.4: "created source uses TubeSync's
        own model defaults/media-profile behavior") -- validate never
        triggers any of this; create always does.
    '''

    def post(self, request, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_create_source_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)

        contract_source_type = body['sourceType']
        canonical_key = body['canonicalKey']
        name = body['name']
        directory = body['directory']
        tubesync_source_type = contract_source_type_to_tubesync(contract_source_type)

        existing = Source.objects.filter(key=canonical_key).first()
        if existing:
            return _conflict(request_id, existing)

        namespace_conflict = (
            Source.objects.filter(name=name).exists()
            or Source.objects.filter(directory=directory).exists()
        )
        if namespace_conflict:
            return _namespace_conflict(request_id)

        form = build_source_form(
            source_type=tubesync_source_type, key=canonical_key,
            name=name, directory=directory,
        )
        run_edit_source_checks(form)
        if not form.is_valid():
            return _invalid(request_id, [str(form.errors)])

        try:
            source = form.save()
        except IntegrityError:
            # get-after-conflict: the pre-checks above narrowed the
            # common case, but a concurrent request may have won the
            # actual DB-level unique constraint in the gap between our
            # pre-check and this save(). Re-query rather than trust the
            # stale pre-check result.
            winner = Source.objects.filter(key=canonical_key).first()
            if winner:
                return _conflict(request_id, winner)
            if (
                Source.objects.filter(name=name).exists()
                or Source.objects.filter(directory=directory).exists()
            ):
                return _namespace_conflict(request_id)
            # Neither key nor name/directory conflict is visible now --
            # an unexpected DB-level failure, not a namespace race.
            raise

        return _json_response(mapping.serialize_source(source), status=201)


def _conflict(request_id, existing_source):
    return error_response(
        status=409,
        code='SOURCE_CONFLICT',
        title='Source already exists',
        detail='A source with this canonical key already exists; adopt its uuid.',
        request_id=request_id,
        retryable=False,
        extra={'existingSourceUuid': str(existing_source.pk)},
    )


def _namespace_conflict(request_id):
    return error_response(
        status=400,
        code='SOURCE_NAMESPACE_CONFLICT',
        title='Name or directory already in use',
        detail=(
            'A different source already uses this name or directory. '
            'This is a genuinely different canonical source and must not '
            'be adopted.'
        ),
        request_id=request_id,
        retryable=False,
    )


class SyncSourceView(BridgeView):
    '''
        POST /sources/{sourceUuid}/sync -- wraps TubeSync's own
        SourceSyncNowView mechanism exactly (same TaskHistory.schedule()
        call, same index_source task, same delay setting). Dedup uses
        sync_dedup.find_pending_or_running_index_task() -- see that
        module's docstring for why get_source_index_task() alone (used by
        upstream's own SourceSyncNowView, which does NOT dedup at all) is
        insufficient per the contract's own instruction.
    '''

    def post(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        source, error = _get_source_or_error(source_uuid, request_id)
        if error:
            return error

        if not find_pending_or_running_index_task(str(source.pk)):
            TaskHistory.schedule(
                index_source,
                str(source.pk),
                delay=index_source.settings.get('delay'),
                vn_fmt=_('Index media from source "{}" once'),
                vn_args=(source.name,),
            )
        # else: a non-completed index_source task already exists for
        # this source -- accepted (202) without scheduling a duplicate.

        return _json_response(mapping.serialize_source(source), status=202)


def _json_response(body, status=200):
    return JsonResponse(body, status=status)
