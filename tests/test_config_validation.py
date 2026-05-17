"""Tests for production configuration safety guard (Settings.validate_for_production).

Covers:
- Startup is blocked when shipped defaults are used with ENVIRONMENT=production
- Each individual insecure field triggers a validation error independently
- Startup succeeds when all secrets are replaced with real values
- Development / staging environments are never blocked by the guard
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is on the path
sys.path.insert(0, str(Path(__file__).parents[1]))

from apps.api.config import Settings

# ── Helper ─────────────────────────────────────────────────────────────────────

def _prod_settings(**overrides) -> Settings:
    """Return a Settings instance with ENVIRONMENT=production and supplied overrides.

    All three sensitive fields are set to safe placeholder values by default;
    individual tests override only the field under test.
    """
    defaults = {
        "environment": "production",
        # Safe placeholder values — not the shipped defaults
        "jwt_secret": "safe-jwt-secret-value-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "admin_api_key": "safe-admin-key-value-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "audit_ip_salt": "safe-audit-salt-value-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ── Development / staging: guard must be a no-op ─────────────────────────────

class TestNonProductionEnvironments:
    def test_development_with_defaults_never_blocked(self):
        """validate_for_production() must not raise in development."""
        settings = Settings(environment="development")
        settings.validate_for_production()  # should not raise

    def test_staging_with_defaults_never_blocked(self):
        """validate_for_production() must not raise in staging."""
        settings = Settings(environment="staging")
        settings.validate_for_production()  # should not raise


# ── Production: insecure defaults must be blocked ────────────────────────────

class TestProductionInsecureDefaultsBlocked:
    def test_default_jwt_secret_blocked(self):
        """Production startup must be blocked when JWT_SECRET is the shipped default."""
        settings = _prod_settings(jwt_secret="change-me-in-production")
        with pytest.raises(ValueError, match="JWT_SECRET"):
            settings.validate_for_production()

    def test_default_admin_api_key_blocked(self):
        """Production startup must be blocked when ADMIN_API_KEY is the shipped default."""
        settings = _prod_settings(admin_api_key="admin-dev-key")
        with pytest.raises(ValueError, match="ADMIN_API_KEY"):
            settings.validate_for_production()

    def test_default_audit_ip_salt_blocked(self):
        """Production startup must be blocked when AUDIT_IP_SALT is the shipped default."""
        settings = _prod_settings(
            audit_ip_salt="change-this-to-a-random-secret-in-production"
        )
        with pytest.raises(ValueError, match="AUDIT_IP_SALT"):
            settings.validate_for_production()

    def test_empty_audit_ip_salt_blocked(self):
        """Production startup must be blocked when AUDIT_IP_SALT is empty string."""
        settings = _prod_settings(audit_ip_salt="")
        with pytest.raises(ValueError, match="AUDIT_IP_SALT"):
            settings.validate_for_production()

    def test_all_defaults_reports_all_violations(self):
        """Error message must list every insecure field when all defaults are used."""
        settings = Settings(
            environment="production",
            jwt_secret="change-me-in-production",
            admin_api_key="admin-dev-key",
            audit_ip_salt="change-this-to-a-random-secret-in-production",
        )
        with pytest.raises(ValueError) as exc_info:
            settings.validate_for_production()
        error_text = str(exc_info.value)
        assert "JWT_SECRET" in error_text
        assert "ADMIN_API_KEY" in error_text
        assert "AUDIT_IP_SALT" in error_text

    def test_error_message_references_runbook(self):
        """The error message should point operators to the runbook."""
        settings = _prod_settings(jwt_secret="change-me-in-production")
        with pytest.raises(ValueError, match="PRODUCTION_RUNBOOK"):
            settings.validate_for_production()


# ── Production: real secrets must pass ───────────────────────────────────────

class TestProductionWithRealSecretsPasses:
    def test_all_real_secrets_pass(self):
        """validate_for_production() must not raise when all secrets are real values."""
        settings = _prod_settings()
        settings.validate_for_production()  # should not raise

    def test_minimal_real_secrets_pass(self):
        """Distinct, non-default values for all three fields must pass validation."""
        settings = Settings(
            environment="production",
            jwt_secret="a" * 64,
            admin_api_key="b" * 64,
            audit_ip_salt="c" * 64,
        )
        settings.validate_for_production()  # should not raise
