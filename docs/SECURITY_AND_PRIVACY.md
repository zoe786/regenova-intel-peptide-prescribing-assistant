# Security and Privacy

## 1. Authentication & Authorisation

REGENOVA-Intel uses two authentication mechanisms:

### JWT Bearer Tokens
- Issued by the auth endpoint (TODO: implement `/auth/token`)
- Signed with `HS256` using `JWT_SECRET`
- Claims: `sub` (user ID), `role` (`researcher | clinician | admin`), `exp`, `iat`
- Expiry: 8 hours (configurable)

### Admin API Key
- Static key from `ADMIN_API_KEY` env var
- Used for: ingest trigger, admin UI backend calls
- Sent as header: `X-Admin-Key: <key>`
- Rotate regularly; never use in production with default value

---

## 2. Role Model

| Role | Description | Permissions |
|------|-------------|-------------|
| `anonymous` | No authentication | No access |
| `researcher` | API key, no clinical context | Read-only chat (no patient context) |
| `clinician` | JWT, verified role | Full chat, patient context, reconstitution (feature flag) |
| `admin` | Admin API key or JWT | All above + ingest, source browser, config |

Role is extracted from JWT `role` claim or inferred from API key type.

---

## 3. JWT Token Structure

```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "user_id_or_email",
    "role": "clinician",
    "iat": 1704067200,
    "exp": 1704096000,
    "jti": "unique-token-id"
  }
}
```

**TODO:** Implement token revocation list (Redis-backed) for immediate invalidation.

---

## 4. API Key Management

- Admin API keys stored as environment variables (never in database)
- Keys should be rotated quarterly in production
- Key format: `rgi-<role>-<random32chars>` (e.g., `rgi-admin-abc123...`)
- Logging: API key usage logged as `key_prefix` (first 8 chars only, never full key)
- Rate limiting: TODO — implement per-key rate limiting

---

## 5. Data Classification

### Vector Store (ChromaDB)
- **Stores:** Anonymised, chunked text from source documents
- **Must NOT store:** Patient identifiers, PHI, PII
- **Chunk metadata:** source_id, source_name, evidence_tier, published_at, jurisdiction
- **Policy:** Run PII detection scan on all ingest inputs before embedding

### Query Logs (Audit)
- **Stores:** Query hash (SHA-256), role, flags triggered, confidence, timestamp
- **Must NOT store:** Raw query text, patient case details, free-text
- **Retention:** 90 days default

### Normalized Documents (filesystem)
- **Stores:** Cleaned, chunked document text
- **Must NOT store:** Patient data, personally identifiable information
- **Classification:** Internal, non-sensitive (source material is public or licensed)

---

## 6. Audit Log Schema

Every significant event is logged with:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "event_type": "chat_query | ingest_trigger | auth_success | auth_failure | safety_flag",
  "request_id": "uuid-v4",
  "session_id": "optional-session-uuid",
  "user_role": "clinician",
  "user_id_hash": "sha256(user_id)",
  "query_hash": "sha256(query_text)",
  "safety_flags": ["SR-001"],
  "confidence_score": 0.72,
  "source_tiers_accessed": [1, 2, 3],
  "latency_ms": 1250,
  "ip_address_hash": "sha256(ip)",
  "environment": "production"
}
```

Audit logs are append-only and written to a separate log sink (stdout JSON, forwarded to SIEM).

---

## 7. Encryption at Rest

- **ChromaDB data** (`data/chroma_db/`): Not encrypted by default. In production, deploy on encrypted volume (AWS EBS with KMS, or similar).
- **Normalized documents** (`data/processed/`): Not encrypted. Deploy on encrypted storage.
- **Graph pickle** (`data/processed/graph.pkl`): Not encrypted. Same as above.
- **Application secrets**: Use AWS Secrets Manager or HashiCorp Vault; never plaintext in `.env` in production.

---

## 8. Encryption in Transit

- All production traffic must use TLS 1.2+
- API-to-ChromaDB: Localhost only in dev; use TLS or VPN in production
- API-to-OpenAI: HTTPS (enforced by OpenAI SDK)
- API-to-Neo4j: Bolt protocol; enable `bolt+s://` in production

---

## 9. Dependency Scanning

```bash
# Scan for known vulnerabilities
pip install safety
safety check -r requirements.txt

# Or with pip-audit
pip install pip-audit
pip-audit
```

Automated scanning: TODO — Add GitHub Actions workflow with `pip-audit` on PR.

---

## 10. Secrets Management

### Development
```bash
cp .env.example .env
# Edit .env locally — never commit
```

### Production
```bash
# AWS Secrets Manager example
aws secretsmanager create-secret --name regenova-intel/prod \
  --secret-string '{"OPENAI_API_KEY":"sk-...","JWT_SECRET":"...","ADMIN_API_KEY":"..."}'

# Retrieve in application (TODO: implement AWS Secrets Manager loader)
```

**Policy:** `.env` files must be in `.gitignore`. Pre-commit hook should scan for `sk-` patterns.

---

## 11. GDPR / HIPAA-Adjacent Considerations

### GDPR (EU users)
- **Data minimisation:** Only source document text is stored; no personal data of end users
- **Right to erasure:** Vector store supports collection deletion; audit logs have retention policy
- **Data residency:** Deploy in EU region if serving EU clinicians (AWS eu-west, Azure westeurope)
- **DPA required:** If processing personal data of EU data subjects (patient cases), a Data Processing Agreement with OpenAI is required

### HIPAA (US users)
- **PHI policy:** Do NOT submit patient PHI to the API query endpoint. The system is not configured for PHI processing.
- **BAA required:** If PHI might be processed, a Business Associate Agreement with OpenAI is required before deployment
- **Minimum necessary:** The system only requires de-identified clinical context; enforce this in clinical workflows
- **Audit controls:** Audit logging meets HIPAA technical safeguard requirements for access logs

### Recommendation
For clinical deployment, obtain legal review and configure the system to:
1. Enforce de-identification of all patient context fields before API submission
2. Process data within the appropriate geographic region
3. Maintain BAAs with all data processors (OpenAI, cloud provider)
