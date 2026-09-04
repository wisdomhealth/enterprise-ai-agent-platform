# Environment and credential ownership

The owner supplies and rotates every production value outside this repository. A
value may be injected only at runtime from the customer-approved secret manager or
deployment configuration. “Platform operations” below is the named customer team,
not a developer account.

| Environment variable | Owner | Description |
|---|---|---|
| `DATABASE_URL` | Platform operations | Application PostgreSQL role and endpoint reference. |
| `MIGRATION_DATABASE_URL` | Platform operations | Separate migration principal and endpoint reference. |
| `POSTGRES_DB` | Platform operations | Production database name. |
| `POSTGRES_USER` | Platform operations | PostgreSQL application-role name. |
| `POSTGRES_PASSWORD` | Platform operations | PostgreSQL role secret from customer secret manager. |
| `POSTGRES_IMAGE` | Platform operations | Approved PostgreSQL 17 pgvector pgBackRest image reference. |
| `POSTGRES_DATA_SOURCE` | Platform operations | Customer-managed durable PostgreSQL volume source. |
| `PGBACKREST_STANZA` | Platform operations | pgBackRest stanza identifier. |
| `PGBACKREST_REPO1_S3_BUCKET` | Data platform | Customer-owned encrypted backup bucket reference. |
| `PGBACKREST_REPO1_S3_ENDPOINT` | Data platform | Backup storage service endpoint reference. |
| `PGBACKREST_REPO1_S3_REGION` | Data platform | Backup storage region. |
| `PGBACKREST_REPO1_S3_URI_STYLE` | Data platform | Approved backup client URI style. |
| `PGBACKREST_REPO1_S3_VERIFY_TLS` | Data platform | Backup endpoint TLS verification policy. |
| `PGBACKREST_REPO1_S3_KEY` | Data platform | Backup repository access identifier. |
| `PGBACKREST_REPO1_S3_KEY_SECRET` | Data platform | Backup repository access secret. |
| `PGBACKREST_REPO1_CIPHER_PASS` | Data platform | Separate pgBackRest encryption secret. |
| `REDIS_URL` | Platform operations | Redis endpoint reference for broker and notifications. |
| `ANTHROPIC_API_KEY` | AI platform | Customer Anthropic account secret. |
| `ANTHROPIC_MODEL` | AI platform | Approved Anthropic model identifier. |
| `SAFETY_CLASSIFIER_MODEL` | AI platform | Approved optional safety classifier identifier. |
| `OPENAI_API_KEY` | AI platform | Customer OpenAI account secret for embeddings. |
| `RERANKER_ENABLED` | Product operations | Approved retrieval reranker feature setting. |
| `GOOGLE_OIDC_CLIENT_ID` | Google Cloud administrator | Staff OIDC client identifier. |
| `GOOGLE_OIDC_CLIENT_SECRET` | Google Cloud administrator | Staff OIDC client secret. |
| `GOOGLE_DRIVE_CLIENT_ID` | Google Cloud administrator | Read-only Drive OAuth client identifier. |
| `GOOGLE_DRIVE_CLIENT_SECRET` | Google Cloud administrator | Read-only Drive OAuth client secret. |
| `GOOGLE_GMAIL_CLIENT_ID` | Google Cloud administrator | Gmail OAuth client identifier. |
| `GOOGLE_GMAIL_CLIENT_SECRET` | Google Cloud administrator | Gmail OAuth client secret. |
| `GMAIL_MESSAGE_ID_DOMAIN` | Email operations | Customer-controlled Message-ID domain. |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud administrator | Customer Google Cloud project identifier. |
| `GOOGLE_KMS_KEY_NAME` | Google Cloud administrator | KMS key resource identifier for envelope wrapping. |
| `APP_ENV` | Platform operations | Runtime environment label. |
| `SELF_HOSTED_FILE_KEY_ALLOWED` | Security owner | Explicitly approved development-only local key mode flag. |
| `CONNECTOR_FILE_KEY_PATH` | Security owner | Approved development-only local key path reference. |
| `SESSION_SECRET` | Security owner | Staff and public-session signing secret. |
| `ERASURE_HASH_KEY` | Privacy owner | Dedicated HMAC key for erasure subject references. |
| `RESTORE_GENERATION` | Recovery commander | Monotonic generation set during restore replay. |
| `STAFF_SESSION_TTL_SECONDS` | Security owner | Approved staff session lifetime. |
| `PUBLIC_BASE_URL` | Platform operations | Customer public HTTPS origin. |
| `INTERNAL_BASE_URL` | Platform operations | Internal backend origin for frontend deployment. |
| `HTTP_PORT` | Network operations | Customer ingress HTTP listener port. |
| `HTTPS_PORT` | Network operations | Customer ingress HTTPS listener port. |
| `TLS_CERT_DIR` | Network operations | Mounted customer TLS certificate directory reference. |
| `GRAFANA_ADMIN_USER` | Observability owner | Customer Grafana administrative user identifier. |
| `GRAFANA_ADMIN_PASSWORD` | Observability owner | Customer Grafana administrative secret. |

## Rotation and access removal

Before sign-off, the customer rotates all inherited development, staging and
bootstrap credentials; verifies each replacement; removes developer production
access; and records the customer owners in the asset register. Rotation includes
OIDC/OAuth clients, provider keys, database roles, KMS/secret-manager access,
backup credentials, TLS administration, Grafana administration and webhook secrets.
