import { createServer } from "node:http";

createServer((request, response) => {
  if (request.url === "/api/v1/auth/me" && request.headers.cookie?.includes("staff_session=")) {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({
        id: "staff-1",
        organization_id: "organization-1",
        email: "reviewer@example.test",
        role: "REVIEWER",
      }),
    );
    return;
  }
  response.writeHead(403, { "Content-Type": "application/json" });
  response.end(JSON.stringify({ detail: "Forbidden" }));
}).listen(3100, "127.0.0.1");
