import os
import sys
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

# Load environment variables from .env file
load_dotenv()


class Config(BaseModel):
    foundry_openai_base_url: str = Field(default_factory=lambda: os.getenv("FOUNDRY_OPENAI_BASE_URL", ""))
    foundry_api_key: str = Field(default_factory=lambda: os.getenv("FOUNDRY_API_KEY", ""))
    foundry_model: str = Field(default_factory=lambda: os.getenv("FOUNDRY_MODEL", ""))

    document_intelligence_endpoint: str = Field(default_factory=lambda: os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT", ""))
    document_intelligence_api_key: str = Field(default_factory=lambda: os.getenv("DOCUMENT_INTELLIGENCE_API_KEY", ""))

    applicationinsights_connection_string: str = Field(
        default_factory=lambda: os.getenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            os.getenv("AZURE_MONITOR_CONNECTION_STRING", "")
        )
    )

    environment: str = Field(default_factory=lambda: os.getenv("ENVIRONMENT", ""))
    enable_sensitive_data: bool = Field(
        default_factory=lambda: os.getenv("ENABLE_SENSITIVE_DATA", "").lower() == "true"
    )

    # Classification & Validation thresholds
    classification_confidence_threshold: float = Field(
        default_factory=lambda: float(os.getenv("CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.65"))
    )
    classification_high_confidence_threshold: float = Field(
        default_factory=lambda: float(os.getenv("CLASSIFICATION_HIGH_CONFIDENCE_THRESHOLD", "0.85"))
    )

    # AI Retry configuration
    max_retries: int = Field(
        default_factory=lambda: int(os.getenv("MAX_RETRIES", "3"))
    )
    retry_backoff_factor: float = Field(
        default_factory=lambda: float(os.getenv("RETRY_BACKOFF_FACTOR", "1.5"))
    )

    @model_validator(mode="after")
    def validate_required_env_vars(self) -> "Config":
        missing = []
        if not self.foundry_openai_base_url:
            missing.append("FOUNDRY_OPENAI_BASE_URL")
        if not self.foundry_api_key:
            missing.append("FOUNDRY_API_KEY")
        if not self.foundry_model:
            missing.append("FOUNDRY_MODEL")
        if not self.environment:
            missing.append("ENVIRONMENT")

        if missing:
            print(
                f"[CONFIG WARNING] The following environment variables are not set:\n"
                + "\n".join(f"  - {v}" for v in missing)
                + f"\nPlease set them in your environment / .env file."
            )

        return self


config = Config()
