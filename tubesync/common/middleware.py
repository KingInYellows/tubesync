from django.conf import settings
from django.forms import BaseForm
from basicauth.middleware import BasicAuthMiddleware as BaseBasicAuthMiddleware


class MaterializeDefaultFieldsMiddleware:
    '''
        Adds 'browser-default' CSS attribute class to all form fields.
    '''

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        return response

    def process_template_response(self, request, response):
        for _, v in getattr(response, 'context_data', {}).items():
            if isinstance(v, BaseForm):
                for _, field in v.fields.items():
                    field.widget.attrs.update({'class':'browser-default'})
        return response


class BasicAuthMiddleware(BaseBasicAuthMiddleware):

    def process_request(self, request):
        bypass_uris = getattr(settings, 'BASICAUTH_ALWAYS_ALLOW_URIS', [])
        # An entry ending in '/' is a path-prefix match (request.path
        # startswith the entry); an entry with no trailing slash keeps the
        # original exact-match behaviour unchanged. This lets a single
        # entry (e.g. medianest_bridge's own namespace) exempt an entire
        # family of routes -- including path-parameterized ones a static
        # tuple of exact strings could never enumerate -- while leaving
        # every existing exact entry (e.g. '/healthcheck') byte-identical
        # to before this change.
        for uri in bypass_uris:
            if uri.endswith('/'):
                if request.path.startswith(uri):
                    return None
            elif request.path == uri:
                return None
        return super().process_request(request)
