# react-email

Hail doesn't render React or store templates — you render a
[react-email](https://react.email) component to a string and send that
string. The body goes out exactly as given: a full
`<!DOCTYPE html>…</html>` document passes through unchanged.

## Render the template

```bash
npm install react-email react react-dom -E
```

```tsx
// Welcome.tsx
import * as React from "react";
import { Html, Body, Text, Button } from "react-email";

export function Welcome({ name }: { name: string }) {
  return (
    <Html lang="en">
      <Body>
        <Text>Hi {name}, welcome aboard.</Text>
        <Button href="https://example.com/start">Get started</Button>
      </Body>
    </Html>
  );
}
```

`render()` is async — it returns a `Promise<string>`. Call it twice: once for
the HTML body, once with `{ plainText: true }` for the text body.

## Send it (Node)

```tsx
// send.tsx
import * as React from "react";
import { render } from "react-email";
import { Welcome } from "./Welcome";

const html = await render(<Welcome name="Ada" />);
const text = await render(<Welcome name="Ada" />, { plainText: true });

const res = await fetch(`${process.env.HAIL_API_URL}/v1/emails`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.HAIL_API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    to: ["ada@example.com"],
    from: "onboarding@yourdomain.com",
    subject: "Welcome aboard",
    body_html: html,
    body_text: text,
    recipient_consent: true,
  }),
});
```

Run it with [`tsx`](https://github.com/privatenumber/tsx) (`npm install -D tsx`; needs `"type": "module"` in `package.json`):

```bash
npx tsx send.tsx
```

## Send it (Python SDK)

There is no JS/TS SDK. Render in Node — at build time, or in a small script —
write the output to a file, and send the string with
[`hail-sdk`](https://pypi.org/project/hail-sdk/):

```python
from pathlib import Path
from hail import Client

html = Path("welcome.html").read_text()

async with Client() as client:  # reads HAIL_API_KEY / HAIL_API_URL from env
    await client.emails.create(
        to=["ada@example.com"],
        from_="onboarding@yourdomain.com",
        subject="Welcome aboard",
        body_html=html,
        recipient_consent=True,
    )
```

## Notes

- Field names are `body_html` / `body_text`. `EmailCreate` in
  [`openapi/openapi.yaml`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml)
  forbids extra fields — `html` / `text` return `422`.
- Open and click tracking only work with an HTML body. A plain-text-only
  email still sends, but opens and clicks are never tracked.
- Hail adds nothing to the body — no footer, no tracking notice. Disclose
  AI use yourself where the law requires it.
- An email with attachments goes out as raw MIME
  ([`core/hailhq/core/providers/email/ses.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/email/ses.py)),
  which adds one trailing line break to each text part — no visible effect.
