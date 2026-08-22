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
        prefix_uris = getattr(settings, 'BASICAUTH_PREFIX_ALLOW_URIS', [])
        # BASICAUTH_ALWAYS_ALLOW_URIS keeps the original exact-match
        # behaviour unchanged. BASICAUTH_PREFIX_ALLOW_URIS is a separate
        # tuple for path-prefix exemptions (request.path startswith the
        # entry) so existing trailing-slash exact entries in
        # BASICAUTH_ALWAYS_ALLOW_URIS are not silently reinterpreted as
        # prefix matches.
        for uri in bypass_uris:
            if request.path == uri:
                return None
        for uri in prefix_uris:
            if request.path.startswith(uri):
                return None
        return super().process_request(request)
