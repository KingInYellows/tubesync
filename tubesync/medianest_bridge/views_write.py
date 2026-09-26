'''
    T3 write endpoints: POST /sources/validate, POST /sources,
    POST /sources/{sourceUuid}/sync. The first mutation surface -- gated
    by MEDIANEST_BRIDGE_READ_ONLY (default true) via the same
    BridgeView.dispatch() every T1/T2 route already uses; nothing here
    adds a separate read-only check.

    No deletion of any kind exists in this app, here or elsewhere.

    Both ValidateSourceView.post and CreateSourceView.post check
    MEDIANEST_BRIDGE_SOURCE_DEFAULTS via _source_defaults_or_error()
    below, the one shared code path around
    config.load_validated_source_defaults() -- so the two views' 503
    PROVIDER_UNAVAILABLE response (envelope, no-echo detail, logging)
    can never diverge. MediaNest's own create flow calls
    POST /sources/validate before POST /sources and treats ANY validate
    failure as a definite, user-retryable failure (never `unknown`) --
    see acquisition-source-write.dispatch.ts's `validate_source_failed`
    path in the MediaNest repo. A broken source-defaults configuration
    therefore fails at validate-time as a real, actionable 503 the user
    can re-submit once an operator fixes it; the create-time 503 remains
    a backstop only (MediaNest's own translateBridgeWriteError has no
    503 case, so that path is reconciled as an unknown outcome instead
    of retried) -- see DECISIONS #54 on the canonical contract.
'''
import json

from django.db import IntegrityError, transaction
from django.http import JsonResponse

from common.logger import log
from sync.models import Source

from . import config, mapping
from .errors import error_response
from .request_schemas import validate_create_source_request, validate_validate_source_request
from .source_forms import (
    build_source_form,
    contract_source_type_to_tubesync,
    extract_form_errors,
    run_edit_source_checks,
    validate_canonical_url,
    validate_source_type_and_key,
)
from .sync_dedup import schedule_sync_now_index
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

        T3 (source-defaults): also checks
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS via the same
        config.load_validated_source_defaults() CreateSourceView.post
        uses, returning the identical 503 PROVIDER_UNAVAILABLE response
        when it's invalid (_source_defaults_or_error() below is the one
        shared code path both views call, so the two can never diverge).
        Checked in the same relative position CreateSourceView.post
        checks it: after the request's own schema/URL-shape errors (a
        caller-fixable 400 always wins over a bridge-configuration 503
        for a request that's malformed on its own terms) and before this
        view's own source_type/key field-level checks (a bridge
        misconfiguration is diagnosed before spending any more work on
        the specific request). This is the 503 that matters for
        MediaNest's own retry semantics: MediaNest calls this endpoint
        before POST /sources and treats any validate failure as a
        definite, user-retryable failure, never `unknown` -- see this
        module's own docstring.
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

        url_errors = validate_canonical_url(
            contract_source_type, canonical_key, canonical_url,
        )
        if url_errors:
            return _invalid(request_id, url_errors)

        # T3: see this class's own docstring for why this runs here --
        # after schema/URL validation, before the field-level checks
        # below -- and _source_defaults_or_error()'s docstring for why
        # this is the one code path CreateSourceView.post also uses.
        _, defaults_error = _source_defaults_or_error(
            request_id, route='POST /sources/validate',
            source_type=contract_source_type,
        )
        if defaults_error:
            return defaults_error

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


def _source_defaults_or_error(request_id, *, route, source_type):
    '''
        Runs config.load_validated_source_defaults() and returns
        (defaults_by_type, None) on success, or (None, error_response)
        on failure -- the one shared code path ValidateSourceView.post
        and CreateSourceView.post both call for their
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS check, so the 503
        PROVIDER_UNAVAILABLE response (envelope, no-echo detail) and the
        log line both views emit on failure can never diverge between
        the two call sites. `route` is only used to label the log line
        (`route` names which endpoint refused the request); it never
        affects the returned response body, which is identical either
        way (_source_defaults_unavailable() below). Only `source_type`'s
        overlay is validated: a broken overlay for the other type does
        not block this request (readiness still reports it).

        MEDIANEST_BRIDGE_SOURCE_DEFAULTS is this bridge's own
        configuration, not something the caller can fix by changing
        their request -- returned as a distinct 5xx (never the
        caller-error 400 the request-shape checks above each call site
        use) so "my request is malformed" and "the bridge is
        misconfigured" are never conflated. Never falls back to plain
        model defaults silently: a broken overlay blocks every
        validate/create until an operator fixes it, matching the
        `sourceDefaults` readiness component reporting the same failure.
    '''
    defaults_by_type, defaults_errors = config.load_validated_source_defaults(
        source_types=(source_type,),
    )
    if defaults_errors:
        log.error(
            'medianest_bridge: refusing %s -- '
            'MEDIANEST_BRIDGE_SOURCE_DEFAULTS is invalid: %s',
            route, '; '.join(defaults_errors),
        )
        return None, _source_defaults_unavailable(request_id)
    return defaults_by_type, None


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

        Fields not supplied by the request (media format, write_nfo,
        copy_thumbnails/copy_channel_images, index_streams, etc.) use
        TubeSync's own Source model defaults overlaid with this bridge's
        own configured profile (T3: config.source_defaults(), env var
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS) -- see build_source_form()'s
        `defaults_overlay` parameter. A broken profile fails this whole
        endpoint with 503 PROVIDER_UNAVAILABLE
        (_source_defaults_or_error() below, shared with
        ValidateSourceView.post) rather than silently reverting to plain
        model defaults.

        This create-time check is a BACKSTOP, not the primary defense:
        MediaNest calls POST /sources/validate first and that endpoint
        now runs the identical check (see ValidateSourceView's own
        docstring), so a broken configuration should already have failed
        there with a 503 MediaNest treats as a definite, re-submittable
        failure. This check stays here too because nothing prevents a
        direct POST /sources call, and because
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS could theoretically change
        between a validate call and the create call that follows it --
        create must never persist under a broken configuration either
        way. A 503 reaching a caller from *this* check specifically has
        no dedicated retry semantics on the MediaNest side (its
        translateBridgeWriteError has no 503 case), so it is reconciled
        as an unknown outcome rather than retried -- the validate-time
        503 above is the one that actually gets a clean, user-visible
        retry.
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
        canonical_url = body['canonicalUrl']
        name = body['name']
        directory = body['directory']
        tubesync_source_type = contract_source_type_to_tubesync(contract_source_type)

        url_errors = validate_canonical_url(
            contract_source_type, canonical_key, canonical_url,
        )
        if url_errors:
            return _invalid(request_id, url_errors)

        # T3: checked before any DB query below -- see
        # _source_defaults_or_error()'s own docstring (the one shared
        # code path ValidateSourceView.post also uses) and this class's
        # docstring for why this create-time check is a backstop behind
        # ValidateSourceView's own identical check, not the primary
        # defense. Its parsed overlays are reused below, so the env var
        # is read once per request.
        defaults_by_type, defaults_error = _source_defaults_or_error(
            request_id, route='POST /sources',
            source_type=contract_source_type,
        )
        if defaults_error:
            return defaults_error

        existing = Source.objects.filter(key=canonical_key).first()
        if existing:
            return _conflict(request_id, existing)

        namespace_conflict = (
            Source.objects.filter(name=name).exists()
            or Source.objects.filter(directory=directory).exists()
        )
        if namespace_conflict:
            return _namespace_conflict(request_id)

        defaults_overlay = defaults_by_type[contract_source_type]
        form = build_source_form(
            source_type=tubesync_source_type, key=canonical_key,
            name=name, directory=directory, defaults_overlay=defaults_overlay,
        )
        if not form.is_valid():
            # A concurrent create may have won the actual DB-level
            # uniqueness check Django's own ModelForm.validate_unique()
            # performs during build_source_form()'s own form.is_valid()
            # call -- reached here, before form.save() and its own
            # IntegrityError recovery below, if the race lands in this
            # earlier window (key/name/directory are all unique=True on
            # Source, so validate_unique() queries for exactly this).
            # Re-query rather than trust the stale pre-check result, so a
            # genuine key collision still returns 409 SOURCE_CONFLICT
            # with the winner's uuid for adoption, not a generic 400 that
            # gives the caller nothing to adopt.
            #
            # Checked BEFORE run_edit_source_checks() deliberately: that
            # function calls form.save(commit=False), which raises
            # ValueError unconditionally whenever form.errors is
            # non-empty (Django's own BaseModelForm.save(), regardless of
            # commit=), for ANY reason -- including this race. Only an
            # already-valid form is safe to hand it.
            winner = Source.objects.filter(key=canonical_key).first()
            if winner:
                return _conflict(request_id, winner)
            if (
                Source.objects.filter(name=name).exists()
                or Source.objects.filter(directory=directory).exists()
            ):
                return _namespace_conflict(request_id)
            return _invalid(request_id, extract_form_errors(form))

        run_edit_source_checks(form)
        if not form.is_valid():
            return _invalid(request_id, extract_form_errors(form))

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


def _source_defaults_unavailable(request_id):
    '''
        The contract's Error.code enum (bridge-openapi.v1.yaml) has no
        code that specifically means "the bridge's own server-side
        configuration is broken" -- PROVIDER_UNAVAILABLE is the closest
        honest fit: it already covers the structurally identical
        "the bridge cannot safely serve this request because of its own
        configuration state" case for a missing/unreadable
        MEDIANEST_BRIDGE_TOKEN_FILE (BridgeView._run_gates()'s 503, same
        code). INTERNAL_PROVIDER_ERROR was considered and rejected: its
        contract description and this app's own use of it (views.py's
        catch-all) are for genuinely unexpected/unhandled failures, not a
        known, named, operator-fixable configuration problem this app
        detected and can describe precisely. Adding a new enum value was
        also considered and rejected for this slice -- it would require a
        contract change for a condition an existing code already
        describes honestly; see this PR's own description for the fuller
        reasoning.

        Shared verbatim by ValidateSourceView.post and
        CreateSourceView.post (via _source_defaults_or_error() above) --
        the wording below deliberately says "create or adopt sources",
        not just "create", since T3 (source-defaults) made this the
        response for both.
    '''
    return error_response(
        status=503,
        code='PROVIDER_UNAVAILABLE',
        title='Bridge source defaults misconfigured',
        detail=(
            'The bridge cannot create or adopt sources until its own '
            'MEDIANEST_BRIDGE_SOURCE_DEFAULTS configuration is fixed; see '
            "this bridge's GET /health/ready sourceDefaults component "
            'for the specific error(s).'
        ),
        request_id=request_id,
        retryable=True,
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
        insufficient per the contract's own instruction, and for the
        current TRACEABILITY obligation #1 verification status (the
        scheduled-not-started case -- including the ADR-0006
        double-indexing scenario, create's 10-minute-delayed task
        followed by an immediate sync-now call -- is dynamically verified
        in this harness; the actively-running case and real-consumer
        concurrency remain owed to M6/T4-T5 integration).
    '''

    def post(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        source, error = _get_source_or_error(source_uuid, request_id)
        if error:
            return error

        with transaction.atomic():
            locked_source = Source.objects.select_for_update().get(pk=source.pk)
            schedule_sync_now_index(locked_source)
        # else: a non-completed index_source task already exists for
        # this source -- accepted (202) without scheduling a duplicate,
        # or a slower scheduled row was advanced to sync-now's delay.

        return _json_response(mapping.serialize_source(source), status=202)


def _json_response(body, status=200):
    return JsonResponse(body, status=status)
