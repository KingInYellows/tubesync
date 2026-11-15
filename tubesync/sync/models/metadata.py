import json
import uuid
from datetime import datetime
from common.json_encoder import JSONEncoder
from common.timestamp import timestamp_to_datetime
from common.utils import django_queryset_generator as qs_gen
from django import db
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ..utils import filter_response
from .media import Media, Source, _aware_utc


def json_normalise(value, /):
    '''
        Returns `value` as it reads back from a `JSONField`.

        `filter_response` builds sets, and yt-dlp hands over tuples and
        other types that `JSONEncoder` writes as lists or strings, so a
        freshly built dict never compares equal to the stored one. A
        round trip through the same encoder makes the comparison fair,
        which is what lets the ingest code skip writes that change nothing.
    '''
    return json.loads(json.dumps(value, cls=JSONEncoder))


def shrink_enabled():
    return getattr(settings, 'SHRINK_NEW_MEDIA_METADATA', False)


def shrink_formats(formats, /):
    '''
        Applies `filter_response` to a list of yt-dlp format dicts.
    '''
    return filter_response(dict(formats=formats), True).get('formats', formats)


class Metadata(db.models.Model):
    '''
        Metadata for an indexed `Media` item.
    '''
    class Meta:
        db_table = 'sync_media_metadata'
        verbose_name = _('Metadata about Media')
        verbose_name_plural = _('Metadata about Media')
        unique_together = (
            ('media', 'site', 'key'),
            ('source', 'site', 'key', ),
        )
        get_latest_by = ["-retrieved", "-created"]

    uuid = db.models.UUIDField(
        _('uuid'),
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        help_text=_('UUID of the metadata'),
    )
    source = db.models.ForeignKey(
        Source,
        on_delete=db.models.CASCADE,
        related_name="videos",
        related_query_name="video",
        help_text=_('Source from which the video was retrieved'),
        blank=True,
        null=True,
    )
    media = db.models.OneToOneField(
        Media,
        # on_delete=models.DO_NOTHING,
        on_delete=db.models.SET_NULL,
        related_name='new_metadata',
        help_text=_('Media the metadata belongs to'),
        blank=True,
        null=True,
        parent_link=False,
    )
    site = db.models.CharField(
        _('site'),
        max_length=256,
        blank=True,
        db_index=True,
        null=False,
        default='Youtube',
        help_text=_('Site from which the metadata was retrieved'),
    )
    key = db.models.CharField(
        _('key'),
        max_length=256,
        blank=True,
        db_index=True,
        null=False,
        default='',
        help_text=_('Media identifier at the site from which the metadata was retrieved'),
    )
    created = db.models.DateTimeField(
        _('created'),
        auto_now_add=True,
        db_index=True,
        help_text=_('Date and time the metadata was created'),
    )
    retrieved = db.models.DateTimeField(
        _('retrieved'),
        db_index=True,
        default=timezone.now,
        help_text=_('Date and time the metadata was retrieved'),
    )
    uploaded = db.models.DateTimeField(
        _('uploaded'),
        db_index=True,
        null=True,
        help_text=_('Date and time the media was uploaded'),
    )
    published = db.models.DateTimeField(
        _('published'),
        db_index=True,
        null=True,
        help_text=_('Date and time the media was published'),
    )
    value = db.models.JSONField(
        _('value'),
        encoder=JSONEncoder,
        null=False,
        default=dict,
        help_text=_('JSON metadata object'),
    )


    def __str__(self):
        template = '"{}" from {} at: {}'
        return template.format(
            self.key,
            self.site,
            self.retrieved.isoformat(timespec='seconds'),
        )

    @db.transaction.atomic(durable=False)
    def ingest_formats(self, formats=list(), /):
        '''
            Stores `formats` as numbered `MetadataFormat` rows.

            Only rows whose value changed are written. A video has dozens
            of formats and the refresh paths call this several times in a
            row with the same list, so rewriting every row each time was
            most of the write load on the database (MED-663).
        '''
        formats = list(formats or ())
        if not formats:
            # An empty list keeps the stored formats, as before.
            return
        format_model = self.format.model
        existing = {
            mdf.number: mdf
            for mdf in self.format.filter(site=self.site, key=self.key)
        }
        to_create = list()
        to_update = list()
        for number, format in enumerate(formats, start=1):
            value = json_normalise(format)
            mdf = existing.get(number)
            if mdf is None:
                to_create.append(format_model(
                    metadata=self,
                    site=self.site,
                    key=self.key,
                    number=number,
                    value=value,
                ))
            elif mdf.value != value:
                mdf.value = value
                to_update.append(mdf)
        if to_create:
            format_model.objects.bulk_create(to_create)
        if to_update:
            format_model.objects.bulk_update(to_update, ['value'])
        stale = [ n for n in existing.keys() if n > len(formats) ]
        if stale:
            # delete any numbers we did not overwrite or create
            self.format.filter(site=self.site, key=self.key, number__in=stale).delete()

    @property
    def with_formats(self):
        formats = self.format.all().order_by('number')
        formats_list = [ f.value for f in qs_gen(formats) ]
        metadata = self.value.copy()
        metadata.update(dict(formats=formats_list))
        return metadata

    @db.transaction.atomic(durable=False)
    def ingest_metadata(self, data):
        assert isinstance(data, dict), type(data)
        if shrink_enabled():
            # Drop expiring format URLs, fragments, caption URLs and the
            # heatmap before anything is stored (MED-663). This is
            # idempotent, so data that was already filtered is unchanged.
            data = filter_response(data, True)
        before = (
            self.retrieved,
            self.published,
            self.uploaded,
            self.value,
        ) if not self._state.adding else None

        try:
            self.retrieved = timestamp_to_datetime(
                self.media.get_metadata_first_value(
                    'epoch',
                    arg_dict=data,
                )
            ) or self.created
        except AssertionError:
            self.retrieved = self.created

        try:
            self.published = timestamp_to_datetime(
                self.media.get_metadata_first_value(
                    ('release_timestamp', 'timestamp',),
                    arg_dict=data,
                )
            )
        except AssertionError:
            self.published = None
        if self.published is None:
            # No real release/upload timestamp in `data` (`timestamp` and
            # `release_timestamp` are both absent, e.g. a source indexed
            # without per-video metadata, or a bare `upload_date`-only
            # import). Fall back to `upload_date` (YYYYMMDD), the same
            # field and parsing `Media.upload_date` uses, rather than an
            # arbitrary ingest/retrieval time -- this is what lets
            # `Media.episode_date` (sync/models/media.py) treat
            # `new_metadata.published` as a stable, SQL-queryable date
            # source for every row that has gone through
            # `ingest_metadata` at all, instead of needing its own
            # `upload_date` fallback branch for them (`upload_date`
            # itself is derived from JSON in Python, so it can't appear
            # in a SQL query). `episode_date` still keeps a narrow
            # `upload_date` fallback of its own, only reachable when no
            # `new_metadata` row exists at all -- see its docstring.
            upload_date = None
            upload_date_str = self.media.get_metadata_first_value(
                'upload_date', arg_dict=data,
            )
            if upload_date_str:
                try:
                    upload_date = datetime.strptime(upload_date_str, '%Y%m%d')
                except (TypeError, ValueError):
                    upload_date = None
            self.published = (
                _aware_utc(upload_date) if upload_date is not None
                else self.media.published or self.retrieved
            )

        value = data.copy() # try not to have side-effects for the caller
        formats_key = self.media.get_metadata_field('formats')
        formats = value.pop(formats_key, list())
        self.value = json_normalise(value)
        self.uploaded = min(
            self.published,
            self.retrieved,
            self.media.created,
        )
        after = (self.retrieved, self.published, self.uploaded, self.value)
        if before != after:
            # Skip the write when nothing changed: the row can be
            # megabytes, and save_to_metadata re-ingests the whole dict
            # to change one key.
            self.save()
        self.ingest_formats(formats)

        return self.with_formats

