import { Page } from "@playwright/test";

const now = "2026-09-03T00:00:00Z";

export async function installPublicChatJourney(page: Page) {
  const sessionId = "26000000-0000-0000-0000-000000000001";
  await page.route("**/api/v1/public/chat/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.endsWith("/sessions")) {
      await route.fulfill({
        json: {
          session: {
            id: sessionId,
            state: "AI_ACTIVE",
            version: 1,
            customer_name: null,
            customer_email: null,
            created_at: now,
            messages: [],
          },
          credential: { token: "task26-browser-only", expires_at: "2030-01-01T00:00:00Z" },
        },
      });
      return;
    }
    if (request.method() === "POST" && url.pathname.endsWith("/messages")) {
      await route.fulfill({
        json: {
          sequence: 1,
          actor: "CUSTOMER",
          body: "What is the refund policy?",
          status: "PERSISTED",
          created_at: now,
        },
      });
      return;
    }
    if (request.method() === "POST" && url.pathname.endsWith("/handoff")) {
      await route.fulfill({ json: { state: "QUEUED" } });
      return;
    }
    if (request.method() === "GET" && url.pathname.endsWith("/events")) {
      const body = [
        "id: 2:1",
        "event: message.segment",
        'data: {"cursor":"2:1","sequence":2,"text":"Refunds take five business days."}',
        "",
        "id: 2:2",
        "event: message.validated",
        'data: {"cursor":"2:2","sequence":2,"citations":[{"title":"Customer policy","section":"Refunds","page_number":2}]}',
        "",
      ].join("\n");
      await route.fulfill({ status: 200, contentType: "text/event-stream", body });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });
}

export async function installAdminOperationsJourney(page: Page, authorized = true) {
  await page.route("**/api/v1/admin/**", async (route) => {
    if (!authorized) {
      await route.fulfill({ status: 404, json: { detail: "not found" } });
      return;
    }
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/operations/summary")) {
      await route.fulfill({
        json: {
          generated_at: now,
          connectors: [
            {
              id: "connector-1",
              kind: "DRIVE",
              status: "ACTIVE",
              updated_at: now,
              requested_scopes: ["drive.readonly"],
            },
          ],
          knowledge_sources: [
            {
              source_id: "source-1",
              status: "ACTIVE",
              root_folder_id: "authorized-root",
              include_descendants: true,
              descendant_count: 2,
              cursor: "cursor-7",
              last_success_at: now,
              backlog: 0,
              isolated_files: 1,
              retry_count: 0,
              recent_error_codes: [],
            },
          ],
          jobs: { queue_depth: 0, failed: 0 },
          support: { backlog: 0 },
          email: { retry_wait: 0, delivery_unknown: 0 },
          rag_quality: null,
          email_quality: null,
        },
      });
      return;
    }
    if (request.method() === "GET" && path.endsWith("/jobs/failed")) {
      await route.fulfill({ json: [] });
      return;
    }
    if (request.method() === "GET" && path.endsWith("/users")) {
      await route.fulfill({
        json: [
          {
            id: "admin-1",
            email: "admin@example.test",
            role: "ADMIN",
            status: "ACTIVE",
            version: 1,
          },
        ],
      });
      return;
    }
    if (request.method() === "GET" && path.endsWith("/retention-policy")) {
      await route.fulfill({
        json: {
          id: "policy-1",
          chat_days: 30,
          email_days: 90,
          audit_days: 365,
          version: 1,
          legal_compliance_guarantee: false,
        },
      });
      return;
    }
    await route.fulfill({ status: 200, json: {} });
  });
}
