from django.urls import path

from .views import CapabilitiesView, HealthLiveView, HealthReadyView, MetaView
from .views_sources import SourceDetailView, SourceLookupView, SourceMediaView

app_name = 'medianest_bridge'

urlpatterns = [

    path('health/live',
         HealthLiveView.as_view(),
         name='health-live'),

    path('health/ready',
         HealthReadyView.as_view(),
         name='health-ready'),

    path('meta',
         MetaView.as_view(),
         name='meta'),

    path('capabilities',
         CapabilitiesView.as_view(),
         name='capabilities'),

    path('sources',
         SourceLookupView.as_view(),
         name='source-lookup'),

    # <str:...>, not Django's <uuid:...> converter: a malformed UUID
    # segment must still reach the view and get the bridge's own 404
    # SOURCE_NOT_FOUND JSON envelope (see views_sources.py's
    # _get_source_or_error), not fall through to Django's ordinary
    # URL-pattern-mismatch 404 HTML page.
    path('sources/<str:source_uuid>',
         SourceDetailView.as_view(),
         name='source-detail'),

    path('sources/<str:source_uuid>/media',
         SourceMediaView.as_view(),
         name='source-media'),

]
