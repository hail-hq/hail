"""One module per carrier that sends SMS webhooks. Each owns its URLs, its
signature check and its payload format. A new SMS carrier adds its module
and its router to ``routers``."""

from hailhq.api.ratelimit import exempt_path
from hailhq.api.routes.sms_webhooks import telnyx, twilio

routers = [twilio.router, telnyx.router]

# A webhook carries the carrier's signature, not an API key.
for _router in routers:
    for _route in _router.routes:
        exempt_path(_route.path)
