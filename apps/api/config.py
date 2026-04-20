"""Application configuration using Pydantic Settings.

Reads all configuration from environment variables or a .env file.
Use get_settings() to obtain a cached singleton instance.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """REGENOVA-Intel application settings.

    All values are read from environment variables.  Refer to .env.example
    for the full list of supported variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ───────────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", description="OpenAI API key")
    llm_model: str = Field(default="gpt-4o", description="LLM model name")
    llm_temperature: float = Field(default=0.1, description="LLM sampling temperature")

    # ── Vector DB ─────────────────────────────────────────────────────────
    vector_db_backend: Literal["chroma", "weaviate", "pinecone"] = Field(
        default="chroma", description="Vector database backend"
    )
    chroma_persist_dir: str = Field(
        default="./data/chroma_db", description="ChromaDB persistence directory"
    )
    weaviate_url: str = Field(
        default="http://localhost:8080", description="Weaviate server URL"
    )
    pinecone_api_key: str = Field(default="", description="Pinecone API key")
    pinecone_env: str = Field(default="", description="Pinecone environment")

    # ── Graph DB ──────────────────────────────────────────────────────────
    neo4j_uri: str = Field(
        default="bolt://localhost:7687", description="Neo4j bolt URI"
    )
    neo4j_user: str = Field(default="neo4j", description="Neo4j username")
    neo4j_password: str = Field(default="changeme", description="Neo4j password")

    # ── App ───────────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0", description="API bind host")
    api_port: int = Field(default=8000, description="API bind port")
    log_level: str = Field(default="INFO", description="Logging level")
    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Deployment environment"
    )
    version: str = Field(default="0.1.0", description="Application version")

    # ── Auth ──────────────────────────────────────────────────────────────
    jwt_secret: str = Field(
        default="change-me-in-production", description="JWT signing secret"
    )
    admin_api_key: str = Field(
        default="admin-dev-key", description="Admin API key"
    )

    # ── Data Paths ────────────────────────────────────────────────────────
    raw_data_dir: str = Field(default="./data/raw", description="Raw data directory")
    processed_data_dir: str = Field(
        default="./data/processed", description="Processed data directory"
    )
    curated_data_dir: str = Field(
        default="./data/curated", description="Curated data directory"
    )

    # ── External APIs ─────────────────────────────────────────────────────
    pubmed_email: str = Field(default="", description="Email for NCBI Entrez")
    pubmed_api_key: str = Field(default="", description="NCBI API key")
    youtube_api_key: str = Field(default="", description="YouTube Data API key")

    # ── Feature Flags ─────────────────────────────────────────────────────
    enable_graph_retrieval: bool = Field(
        default=False, description="Enable graph-hybrid retrieval"
    )
    enable_reconstitution_guidance: bool = Field(
        default=False, description="Enable reconstitution guidance endpoint"
    )

    # ── CORS ──────────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default=["http://localhost:8501", "http://localhost:3000"],
        description="Allowed CORS origins",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Using lru_cache ensures the .env file is read only once and the same
    Settings object is shared across the application.
    """
    settings = Settings()
    logger.info(
        "Settings loaded: env=%s model=%s vector_backend=%s",
        settings.environment,
        settings.llm_model,
        settings.vector_db_backend,
    )
    return settings
