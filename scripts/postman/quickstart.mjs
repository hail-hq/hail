const variable = (name) => `{{${name}}}`;

export const quickstartVariables = [
  ["country_code", "US"],
  ["number_type", "local"],
  ["quote_id", ""],
  ["expected_total_cents", "0"],
  ["number_id", ""],
  ["agent_id", ""],
  ["verification_id", ""],
  ["domain_id", ""],
  ["email_id", ""],
  ["recipient_phone", ""],
  ["recipient_email", ""],
  ["custom_domain", "example.com"],
  ["enable_paid_requests", "false"],
].map(([key, value]) => ({ key, value, type: "string" }));

function request(name, method, path, description, body, paid = false) {
  const item = {
    name,
    request: {
      method,
      header: [],
      url: `{{baseUrl}}${path}`,
      description,
    },
    response: [],
  };
  if (body) {
    item.request.header.push({
      key: "Content-Type",
      value: "application/json",
    });
    item.request.body = {
      mode: "raw",
      // Keep the price check numeric after Postman expands the variable.
      raw: JSON.stringify(body, null, 2).replace(
        '"{{expected_total_cents}}"',
        "{{expected_total_cents}}",
      ),
      options: { raw: { language: "json" } },
    };
  }
  if (paid)
    item.event = [
      {
        listen: "prerequest",
        script: {
          type: "text/javascript",
          exec: [
            'if (pm.variables.get("enable_paid_requests") !== "true") {',
            '  console.warn("Skipped: set enable_paid_requests to true to spend Hail credit.");',
            "  pm.execution.skipRequest();",
            "}",
          ],
        },
      },
    ];
  return item;
}

// Save values from a successful response for the next steps.
function saves(item, status, lines) {
  item.event = [
    ...(item.event ?? []),
    {
      listen: "test",
      script: {
        type: "text/javascript",
        exec: [
          `if (pm.response.code === ${status}) {`,
          "  const body = pm.response.json();",
          ...lines.map((line) => `  ${line}`),
          "}",
        ],
      },
    },
  ];
  return item;
}

const saveId = (item, name) =>
  saves(item, 201, [`pm.collectionVariables.set("${name}", body.id);`]);

export function quickstartItems() {
  const health = request(
    "Check API health",
    "GET",
    "/healthz",
    "No API key or credit needed.",
  );
  health.request.auth = { type: "noauth" };
  return [
    {
      name: "1. Connect",
      item: [
        health,
        request(
          "List my agents",
          "GET",
          "/v1/agents",
          "Check your Hail key without spending credit.",
        ),
        request(
          "List my numbers",
          "GET",
          "/v1/numbers",
          "Read the numbers in your workspace.",
        ),
      ],
    },
    {
      name: "2. Find a number",
      item: [
        saves(
          request(
            "Get number quotes",
            "POST",
            "/v1/numbers/quotes",
            "Read-only. Saves the recommended offer's quote_id, number_type, and expected_total_cents (monthly_cents + setup_cents). To pick another offer, copy its values to the variables. Quotes expire in 10 minutes. Offers may need verification first.",
            {
              country_code: variable("country_code"),
              capabilities: ["voice", "sms"],
            },
          ),
          200,
          [
            "const offer = body.offers.find((o) => o.quote_id === body.recommended_quote_id) ?? body.offers[0];",
            "if (offer) {",
            '  pm.collectionVariables.set("quote_id", offer.quote_id);',
            '  pm.collectionVariables.set("number_type", offer.number_type);',
            '  pm.collectionVariables.set("expected_total_cents", offer.monthly_cents + offer.setup_cents);',
            "}",
          ],
        ),
        request(
          "List states and provinces",
          "GET",
          "/v1/numbers/regions",
          "Lists US states and Canadian provinces. To search by city, add city and a returned region code to the Get number quotes body.",
        ),
        saveId(
          request(
            "Get a quoted number",
            "POST",
            "/v1/numbers",
            "Spends credit: first month's rent plus setup. Uses the offer saved by Get number quotes. Saves the returned id as number_id. Uses a price check, so the server refuses a different total.",
            {
              quote_id: variable("quote_id"),
              country_code: variable("country_code"),
              number_type: variable("number_type"),
              expected_total_cents: variable("expected_total_cents"),
            },
            true,
          ),
          "number_id",
        ),
      ],
    },
    {
      name: "3. Set up an assistant",
      item: [
        saveId(
          request(
            "Create an assistant",
            "POST",
            "/v1/agents",
            "Creates a saved agent; it does not buy a number or place a call. Saves the returned id as agent_id. Use a unique name if you repeat this request.",
            {
              name: "Postman test assistant",
              system_prompt:
                "Help callers with appointment questions. Ask for details when you need them. Do not invent bookings.",
              first_message: "Hello. How can I help with your appointment?",
              voice_config: { language: "en" },
              voice_enabled: true,
              sms_enabled: true,
            },
          ),
          "agent_id",
        ),
        request(
          "Read an assistant",
          "GET",
          "/v1/agents/{{agent_id}}",
          "Read the agent you created.",
        ),
        request(
          "Route calls and texts",
          "PATCH",
          "/v1/numbers/{{number_id}}",
          "Assign a number with both voice and SMS to this agent. Calls and texts to the number are now answered by the agent.",
          {
            voice_agent_id: variable("agent_id"),
            sms_agent_id: variable("agent_id"),
          },
        ),
      ],
    },
    {
      name: "4. Check verification",
      item: [
        request(
          "Read number requirements",
          "GET",
          "/v1/verifications/requirements?country_code={{country_code}}&number_type={{number_type}}&purpose=number",
          "Read the current fields and documents before submitting any personal details. Add provider and subject_type query fields to match your chosen offer and holder. The full reference covers multipart submission; this quickstart does not send documents.",
        ),
        saves(
          request(
            "List verification submissions",
            "GET",
            "/v1/verifications",
            "Read number-verification and SMS-registration states. Saves the first id as verification_id.",
          ),
          200,
          [
            'if (body[0]) pm.collectionVariables.set("verification_id", body[0].id);',
          ],
        ),
        request(
          "Read a verification",
          "GET",
          "/v1/verifications/{{verification_id}}",
          "Uses verification_id from the list. Some carrier reviews take business days.",
        ),
        request(
          "Read US texting fees",
          "GET",
          "/v1/verifications/sms-fees",
          "Read registration and monthly fees before filing a US SMS registration.",
        ),
      ],
    },
    {
      name: "5. Try email",
      item: [
        request(
          "List email identities",
          "GET",
          "/v1/email-domains",
          "Read verified senders and the default from address.",
        ),
        saveId(
          request(
            "Register my own domain",
            "POST",
            "/v1/email-domains",
            "Replace custom_domain with a domain you own. Publish the returned DNS records, then use Verify my domain. Saves the returned id as domain_id.",
            {
              kind: "custom",
              domain: variable("custom_domain"),
            },
          ),
          "domain_id",
        ),
        request(
          "Check my domain DNS",
          "GET",
          "/v1/email-domains/{{domain_id}}/dns-check",
          "Observe DNS records and the DNS host. This does not change verification state.",
        ),
        request(
          "Verify my domain",
          "POST",
          "/v1/email-domains/{{domain_id}}/verify",
          "Recheck the provider's view after publishing DNS records.",
        ),
        saveId(
          request(
            "Send a test email",
            "POST",
            "/v1/emails",
            "Spends credit. Set recipient_email to a consenting test recipient. With multiple verified identities, add from to choose one. With no identity, Hail Cloud can create its default Hail address. Saves the returned id as email_id.",
            {
              to: [variable("recipient_email")],
              subject: "Hello from Hail",
              body_text: "This is a test sent from Postman.",
              recipient_consent: true,
            },
            true,
          ),
          "email_id",
        ),
        request(
          "Read received email",
          "GET",
          "/v1/emails?direction=inbound&limit=20",
          "List received messages. Configure receiving for your domain before expecting inbound mail.",
        ),
        request(
          "Read email delivery events",
          "GET",
          "/v1/emails/{{email_id}}/events",
          "Uses email_id from Send a test email, or set it from a received email. Events include delivery and, when enabled, opens and clicks.",
        ),
      ],
    },
    {
      name: "6. Try calls and texts",
      item: [
        request(
          "Send a test text",
          "POST",
          "/v1/sms",
          "Spends credit. Set recipient_phone to a consenting test recipient in E.164 form. A US long code texting a US number needs an approved SMS registration. Add from to choose your sending number.",
          {
            to: variable("recipient_phone"),
            body: "Hello from Hail and Postman.",
            recipient_consent: true,
          },
          true,
        ),
        request(
          "Call with my assistant",
          "POST",
          "/v1/calls",
          "Spends credit. Set recipient_phone. The assistant calls the consenting recipient using its saved instructions.",
          {
            to: variable("recipient_phone"),
            agent_id: variable("agent_id"),
            recipient_consent: true,
          },
          true,
        ),
        request(
          "Read recent calls",
          "GET",
          "/v1/calls?limit=20",
          "Read call states and results.",
        ),
        request(
          "Read recent texts",
          "GET",
          "/v1/sms?limit=20",
          "Read inbound and outbound texts.",
        ),
      ],
    },
  ];
}
