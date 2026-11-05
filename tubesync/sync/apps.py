from django.apps import AppConfig


class SyncConfig(AppConfig):

    name = 'sync'

    def ready(self):
        # Fork change (MED-634): upstream connects the receivers in
        # sync/signals.py only as a side effect of importing
        # sync/views/sources.py, which happens when the URLconf loads (on
        # the first web request, or during the system checks most
        # management commands run). `manage.py shell` skips those checks,
        # so a Source or Media saved there fired no receivers: changing a
        # source's quality settings scheduled no "Checking all media"
        # task and skipped media stayed skipped. Connect them here, the
        # standard Django place, so every process has them.
        from . import signals # noqa: F401
