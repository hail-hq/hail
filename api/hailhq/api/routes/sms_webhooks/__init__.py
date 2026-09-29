"""One module per carrier that sends SMS webhooks. Each owns its URLs, its
signature check and its payload format. A new SMS carrier adds its module
and its router to ``routers``."""

from hailhq.api.routes.sms_webhooks import telnyx, twilio

routers = [twilio.router, telnyx.router]
