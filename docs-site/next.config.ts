import { createMDX } from "fumadocs-mdx/next";
import type { NextConfig } from "next";

const withMDX = createMDX();

// API-reference page slugs are the OpenAPI operationIds. Mounting the customer
// routers under /v1 (PR #89) inserted `_v1` into every FastAPI-generated id, so
// the old /docs/api/<id> URLs (and their .md twins) now redirect to the new ones.
const RENAMED_OPERATION_IDS: Record<string, string> = {
  create_call_calls_post: "create_call_v1_calls_post",
  list_calls_calls_get: "list_calls_v1_calls_get",
  get_call_calls__call_id__get: "get_call_v1_calls__call_id__get",
  create_email_emails_post: "create_email_v1_emails_post",
  list_emails_emails_get: "list_emails_v1_emails_get",
  list_email_events_emails__email_id__events_get:
    "list_email_events_v1_emails__email_id__events_get",
  get_email_stats_emails_stats_get: "get_email_stats_v1_emails_stats_get",
  get_email_emails__email_id__get: "get_email_v1_emails__email_id__get",
  get_email_raw_emails__email_id__raw_get:
    "get_email_raw_v1_emails__email_id__raw_get",
  get_email_attachment_emails__email_id__attachments__attachment_id__get:
    "get_email_attachment_v1_emails__email_id__attachments__attachment_id__get",
  list_events_events_get: "list_events_v1_events_get",
  create_email_domain_email_domains_post:
    "create_email_domain_v1_email_domains_post",
  list_email_domains_email_domains_get:
    "list_email_domains_v1_email_domains_get",
  check_domain_email_domains_check_domain_get:
    "check_domain_v1_email_domains_check_domain_get",
  get_email_domain_email_domains__domain_id__get:
    "get_email_domain_v1_email_domains__domain_id__get",
  patch_email_domain_email_domains__domain_id__patch:
    "patch_email_domain_v1_email_domains__domain_id__patch",
  delete_email_domain_email_domains__domain_id__delete:
    "delete_email_domain_v1_email_domains__domain_id__delete",
  verify_email_domain_email_domains__domain_id__verify_post:
    "verify_email_domain_v1_email_domains__domain_id__verify_post",
  acquire_number_numbers_post: "acquire_number_v1_numbers_post",
  list_numbers_numbers_get: "list_numbers_v1_numbers_get",
  release_number_numbers__number_id__delete:
    "release_number_v1_numbers__number_id__delete",
  get_number_numbers__number_id__get: "get_number_v1_numbers__number_id__get",
  enable_sms_numbers__number_id__enable_sms_post:
    "enable_sms_v1_numbers__number_id__enable_sms_post",
  create_subscription_webhooks_post: "create_subscription_v1_webhooks_post",
  list_subscriptions_webhooks_get: "list_subscriptions_v1_webhooks_get",
  get_subscription_webhooks__sub_id__get:
    "get_subscription_v1_webhooks__sub_id__get",
  patch_subscription_webhooks__sub_id__patch:
    "patch_subscription_v1_webhooks__sub_id__patch",
  delete_subscription_webhooks__sub_id__delete:
    "delete_subscription_v1_webhooks__sub_id__delete",
  rotate_secret_webhooks__sub_id__rotate_secret_post:
    "rotate_secret_v1_webhooks__sub_id__rotate_secret_post",
  list_deliveries_webhooks__sub_id__deliveries_get:
    "list_deliveries_v1_webhooks__sub_id__deliveries_get",
  redeliver_webhooks__sub_id__deliveries__delivery_id__redeliver_post:
    "redeliver_v1_webhooks__sub_id__deliveries__delivery_id__redeliver_post",
  unsubscribe_unsubscribe_get: "unsubscribe_v1_unsubscribe_get",
  create_sms_sms_post: "create_sms_v1_sms_post",
  list_sms_sms_get: "list_sms_v1_sms_get",
  list_sms_suppressions_sms_suppressions_get:
    "list_sms_suppressions_v1_sms_suppressions_get",
  delete_sms_suppression_sms_suppressions__number__delete:
    "delete_sms_suppression_v1_sms_suppressions__number__delete",
  get_sender_id_sms_sender_id_get: "get_sender_id_v1_sms_sender_id_get",
  patch_sender_id_sms_sender_id_patch: "patch_sender_id_v1_sms_sender_id_patch",
  get_sms_sms__sms_id__get: "get_sms_v1_sms__sms_id__get",
  list_contacts_contacts_get: "list_contacts_v1_contacts_get",
  create_contact_contacts_post: "create_contact_v1_contacts_post",
  patch_contact_contacts__contact_id__patch:
    "patch_contact_v1_contacts__contact_id__patch",
  delete_contact_contacts__contact_id__delete:
    "delete_contact_v1_contacts__contact_id__delete",
  put_member_phone_members__user_id__phone_put:
    "put_member_phone_v1_members__user_id__phone_put",
  delete_member_phone_members__user_id__phone_delete:
    "delete_member_phone_v1_members__user_id__phone_delete",
  get_whoami_whoami_get: "get_whoami_v1_whoami_get",
};

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Served at hail.so/docs via a cross-zone rewrite from the marketing app.
  // basePath keeps this app's /_next/* assets namespaced so they can't collide
  // with the apex app's, and makes the standalone deployment browsable too.
  basePath: "/docs",
  async rewrites() {
    // Every doc page and every generated API-reference page is available as
    // plain markdown by appending `.md` — e.g. /docs/architecture.md,
    // /docs/api/create_email_v1_emails_post.md. Handled by app/llms.mdx/[[...slug]],
    // which knows both sources; this just maps the friendly suffix onto it.
    return [{ source: "/:path*.md", destination: "/llms.mdx/:path*" }];
  },
  async redirects() {
    // Aug 2026 IA reorg: cloud docs moved to the top level, provisioning docs
    // under self-host/. Sources are basePath-relative (Next prefixes /docs).
    // 308 — the old URLs were live and indexed; the move is permanent.
    return [
      { source: "/setup", destination: "/self-host", permanent: true },
      { source: "/setup/mcp", destination: "/mcp", permanent: true },
      { source: "/setup/webhooks", destination: "/webhooks", permanent: true },
      {
        source: "/setup/twilio",
        destination: "/self-host/twilio",
        permanent: true,
      },
      {
        source: "/setup/livekit-cloud",
        destination: "/self-host/livekit-cloud",
        permanent: true,
      },
      {
        source: "/setup/aws-ses",
        destination: "/self-host/aws-ses",
        permanent: true,
      },
      {
        source: "/operations",
        destination: "/self-host/operations",
        permanent: true,
      },
      // Redirects run before the `.md` rewrite above, and `/api/old` does not
      // match `/api/old.md`, so both forms need their own entry.
      ...Object.entries(RENAMED_OPERATION_IDS).flatMap(([oldId, newId]) => [
        {
          source: `/api/${oldId}`,
          destination: `/api/${newId}`,
          permanent: true,
        },
        {
          source: `/api/${oldId}.md`,
          destination: `/api/${newId}.md`,
          permanent: true,
        },
      ]),
    ];
  },
};

export default withMDX(nextConfig);
