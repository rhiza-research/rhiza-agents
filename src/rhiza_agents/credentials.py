"""User-scoped credentials: encryption at rest + output redaction.

The credentials feature is opt-in: the encryption key comes from the
``CREDENTIAL_ENCRYPTION_KEY`` env var (a 32-byte urlsafe-base64 Fernet key).
When the var is unset, ``credentials_enabled()`` returns False and all
encrypt/decrypt calls raise — the API layer must short-circuit to a 503
in that case so the feature fails closed.

Stored credentials are flat name/value pairs. The LLM never sees values;
it only sees the available *names* via the system prompt and approval
card. Values are injected into sandbox executions at run time per the
materialization plan the LLM provides in its tool call.
"""

import base64
import logging
import os
import re

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


_fernet: Fernet | None = None
_fernet_initialized = False


def _get_fernet() -> Fernet | None:
    """Lazily build the Fernet instance from CREDENTIAL_ENCRYPTION_KEY.

    Returns None when the env var is unset — callers must treat that as
    "credentials feature disabled" and refuse to store or read secrets.
    """
    global _fernet, _fernet_initialized
    if _fernet_initialized:
        return _fernet
    _fernet_initialized = True
    raw = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not raw:
        logger.warning("CREDENTIAL_ENCRYPTION_KEY not set — credentials feature disabled")
        return None
    try:
        # Fernet keys are urlsafe-base64-encoded 32-byte strings.
        _fernet = Fernet(raw.encode())
    except Exception:
        try:
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) != 32:
                raise ValueError(f"key must decode to 32 bytes, got {len(decoded)}")
            _fernet = Fernet(base64.urlsafe_b64encode(decoded))
        except Exception as e:
            logger.error("CREDENTIAL_ENCRYPTION_KEY is invalid: %s", e)
            return None
    return _fernet


def credentials_enabled() -> bool:
    """Return True if the encryption key is configured and the feature is live."""
    return _get_fernet() is not None


def encrypt_value(value: str) -> bytes:
    """Encrypt a single secret value.

    Raises RuntimeError if the encryption key is not configured.
    """
    f = _get_fernet()
    if f is None:
        raise RuntimeError("credentials feature is disabled (CREDENTIAL_ENCRYPTION_KEY not set)")
    return f.encrypt(value.encode("utf-8"))


def decrypt_value(ciphertext: bytes) -> str:
    """Decrypt a previously-encrypted secret value back to a string.

    Raises RuntimeError if the encryption key is not configured, or
    cryptography's InvalidToken if the ciphertext was produced with a
    different key.
    """
    f = _get_fernet()
    if f is None:
        raise RuntimeError("credentials feature is disabled (CREDENTIAL_ENCRYPTION_KEY not set)")
    if isinstance(ciphertext, memoryview):
        ciphertext = bytes(ciphertext)
    return f.decrypt(ciphertext).decode("utf-8")


# A {NAME} placeholder in a file content template. Names follow common
# environment-variable conventions (letters, digits, underscore; not
# starting with a digit) so we don't accidentally substitute braces in
# arbitrary file content.
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def extract_placeholders(template: str) -> list[str]:
    """Return the ordered list of ``{NAME}`` placeholder names in a template."""
    return PLACEHOLDER_RE.findall(template or "")


def substitute_placeholders(template: str, values: dict[str, str]) -> str:
    """Replace ``{NAME}`` placeholders in ``template`` with values from ``values``.

    Unknown placeholders are left untouched and a warning is logged. Caller
    is expected to have validated that every placeholder has a corresponding
    secret before calling this.
    """

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        logger.warning("Template references unknown placeholder %r", name)
        return match.group(0)

    return PLACEHOLDER_RE.sub(_sub, template)


def redact_output(text: str, secret_values: list[str]) -> str:
    """Replace verbatim occurrences of any secret value with ``[REDACTED]``.

    Backstop only: the structural defense is that the LLM never sees the
    values to begin with. This catches accidental leaks via stdout, stderr,
    or captured output files. Sophisticated transformations (base64,
    slicing, etc.) defeat it; do not rely on this for adversarial scripts.
    """
    if not text or not secret_values:
        return text
    # Replace longer values first so a value that contains another value
    # gets redacted as a single unit.
    for value in sorted({v for v in secret_values if v}, key=lambda v: -len(v)):
        if value in text:
            text = text.replace(value, "[REDACTED]")
    return text
