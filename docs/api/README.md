# API contract

`openapi.json` is generated from the implemented FastAPI routes; do not hand-edit
it. Regenerate after route or schema changes:

```bash
scripts/export-openapi --output docs/api/openapi.json
scripts/check-documentation
```

Each operation includes `x-security-classification`:

- `public`: browser/OAuth bootstrap or a public-chat route. Public chat routes
  that address an existing session require the `PublicChatCredential` bearer.
- `staff`: approved staff session, and CSRF/origin protection on mutations.
- `administrator`: staff session plus the route's administrator/resource grant.
- `internal`: not a public customer API; the deployment ingress must restrict it.

The schema defines `StaffSession` and `PublicChatCredential`. Health, metrics,
public, staff and administrator routes are exported together so handoff reviewers
can compare the exact implementation with deployment controls.
