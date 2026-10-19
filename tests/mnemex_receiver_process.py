# ruff: noqa: E402
# Isolate storage and provider environment before importing Django or MNEMEX.
"""Owned loopback test peer running the actual installed MNEMEX Django inbox.

Never imported by the application. It refuses existing scratch storage and
sets isolated settings before importing Django or the receiver.
"""

import hashlib
import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

root = Path(sys.argv[1]).resolve()
root.mkdir(mode=0o700, exist_ok=False)
os.environ['DJANGO_SETTINGS_MODULE'] = 'mnemex.web.settings.test'
for key in tuple(os.environ):
    if any(name in key for name in ('SUPABASE', 'DATABASE_URL', 'VISION_PROVIDER', 'SMTP')):
        os.environ.pop(key)

import django

django.setup()
from wsgiref.simple_server import WSGIRequestHandler, make_server

from django.conf import settings
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.utils import timezone

settings.DATABASES['default']['NAME'] = str(root/'receiver-test.sqlite3')
settings.MNEMEX_SHOW_RESULTS_ENABLED = True
settings.ALLOWED_HOSTS = ['127.0.0.1']
call_command('migrate', interactive=False, verbosity=0)
from mnemex.partners.models import (
    PartnerClient,
    PartnerOrganization,
    ShowClientCredential,
    ShowSource,
)

org = PartnerOrganization.objects.create(name='Synthetic cross-repository receiver')
client = PartnerClient.objects.create(organization=org, name='show', environment='sandbox')
ShowSource.objects.create(client=client, source_show_id='synthetic:missoula')
token=secrets.token_urlsafe(48)
ShowClientCredential.objects.create(client=client, source_show_id='synthetic:missoula', token_sha256=hashlib.sha256(token.encode()).hexdigest(), expires_at=timezone.now()+timedelta(hours=1))

class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass

with make_server('127.0.0.1',0,get_wsgi_application(),handler_class=QuietHandler) as server:
    ready=root/'ready.json'
    with ready.open('x') as output:
        os.fchmod(output.fileno(),0o600)
        json.dump({'url':f'http://127.0.0.1:{server.server_port}/partners/results/ingest', 'token':token},output)
    server.serve_forever()
