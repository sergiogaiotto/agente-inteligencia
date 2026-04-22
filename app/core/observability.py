"""Módulo de observabilidade com LangFuse v4.x."""

from app.core.config import get_settings
from functools import lru_cache
import logging

logger = logging.getLogger(__name__)

# ── Import seguro — compatível com langfuse v2.x, v3.x e v4.x ──
_Langfuse = None
_CallbackHandler = None

try:
    from langfuse import Langfuse as _Langfuse
except ImportError:
    logger.warning("langfuse não instalado — observabilidade desativada.")

try:
    # v4.x
    from langfuse.langchain import CallbackHandler as _CallbackHandler
except ImportError:
    try:
        # v2.x / v3.x
        from langfuse.callback import CallbackHandler as _CallbackHandler
    except ImportError:
        try:
            from langfuse import CallbackHandler as _CallbackHandler
        except (ImportError, AttributeError):
            logger.warning("CallbackHandler do langfuse não disponível. Instale: pip install langfuse langchain")


def _is_configured() -> bool:
    settings = get_settings()
    key = settings.langfuse_public_key
    return bool(key and not key.startswith("pk-your"))


@lru_cache()
def get_langfuse_client():
    """Retorna cliente LangFuse configurado."""
    if not _Langfuse or not _is_configured():
        return None
    settings = get_settings()
    try:
        return _Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as e:
        logger.warning(f"Falha ao inicializar LangFuse: {e}")
        return None


def get_langfuse_handler(
    session_id: str = None,
    user_id: str = None,
    trace_name: str = "agent_execution",
):
    """Retorna callback handler do LangFuse para LangChain."""
    if not _CallbackHandler or not _is_configured():
        return None
    settings = get_settings()
    try:
        # v4.x+ usa apenas public_key + host (secret via env ou sem)
        return _CallbackHandler(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            session_id=session_id,
            user_id=user_id,
            trace_name=trace_name,
        )
    except TypeError:
        # Versão mais nova pode não aceitar secret_key diretamente
        try:
            import os
            os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
            os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
            os.environ["LANGFUSE_HOST"] = settings.langfuse_host
            return _CallbackHandler(
                session_id=session_id,
                user_id=user_id,
                trace_name=trace_name,
            )
        except Exception:
            try:
                return _CallbackHandler()
            except Exception:
                return None
    except Exception as e:
        logger.warning(f"Falha ao criar CallbackHandler: {e}")
        return None


class ObservabilityTracker:
    """Tracker centralizado — degrada silenciosamente se LangFuse indisponível."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = get_langfuse_client()
        return self._client

    def create_trace(self, name: str, metadata: dict = None, **kwargs):
        if not self.client:
            return None
        try:
            return self.client.trace(name=name, metadata=metadata or {}, **kwargs)
        except Exception:
            return None

    def log_generation(self, trace, name: str, input_text: str, output_text: str, model: str, **kwargs):
        if not trace:
            return None
        try:
            return trace.generation(name=name, input=input_text, output=output_text, model=model, **kwargs)
        except Exception:
            return None

    def log_span(self, trace, name: str, input_data: dict = None, output_data: dict = None):
        if not trace:
            return None
        try:
            return trace.span(name=name, input=input_data, output=output_data)
        except Exception:
            return None

    def flush(self):
        if self.client:
            try:
                self.client.flush()
            except Exception:
                pass


tracker = ObservabilityTracker()