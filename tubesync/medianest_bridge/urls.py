from django.urls import path

from .views import CapabilitiesView, HealthLiveView, HealthReadyView, MetaView

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

]
