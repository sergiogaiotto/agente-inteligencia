"""Manutenção dos arquivos de log (Onda Observabilidade).

Endpoints para evitar entupimento do storage:
- GET    /api/v1/observability/logs/stats               → tamanho/mtime/disk usage
- GET    /api/v1/observability/logs/tail/{file}?lines=N → debug ad-hoc
- POST   /api/v1/observability/logs/clear/{file}        → trunca conteúdo
- POST   /api/v1/observability/logs/rotate              → força rotação
- DELETE /api/v1/observability/logs/archives            → apaga backups antigos

Convenções:
- Path safety: só aceita nomes de arquivo do whitelist (app/tabular/api/
  audit/errors) — bloqueia path traversal.
- Auth: `Depends(require_user)` em TODOS. `clear` e `delete` exigem role=root.
- Audit: cada ação grava em audit_repo.

Decisões:
- "Truncar" NÃO deleta o arquivo — abre em modo 'w' e fecha. Handler
  continua escrevendo nele sem precisar reabrir (file descriptor preservado).
- "Rotate" força o TimedRotatingFileHandler.doRollover() de cada handler.
- "Delete archives" apaga arquivos com sufixo de data (ex: app.log.2026-05-23)
  mas mantém os arquivos atuais (app.log, tabular.log, etc).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import require_user
from app.core.database import audit_repo

logger = logging.getLogger("app.api")
router = APIRouter(prefix="/api/v1/observability/logs", tags=["observability"])


# Whitelist canônica dos 5 arquivos de log + retenção configurada
# (espelhado de logging_setup._LOG_FILES — fonte única de verdade seria
# importar, mas aqui ficamos com cópia explícita pra desacoplar UI).
_LOG_FILES_META = {
    # Retenção uniforme 7d alinhada a logging_setup._LOG_FILES (2026-05-27).
    # Se mudar lá, mudar aqui também — a UI lê esta cópia. Idealmente seria
    # 1 fonte de verdade compartilhada, mas evitamos import circular.
    "app":     {"retention_days": 7,  "description": "Geral da aplicação"},
    "tabular": {"retention_days": 7,  "description": "Onda Tabular (analyze/promote/append/query)"},
    "api":     {"retention_days": 7,  "description": "Request/response HTTP"},
    "audit":   {"retention_days": 7,  "description": "Writes em DB (compliance — tabela `audit_log` é source of truth)"},
    "errors":  {"retention_days": 7,  "description": "Apenas ERROR+ (escalation)"},
}

# Limites defensivos para tail
_TAIL_MAX_LINES = 1000
_TAIL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB de janela máxima


def _log_dir() -> Path:
    """Pasta raiz dos logs (configurável via env, default 'logs')."""
    return Path(os.environ.get("LOG_DIR", "logs"))


def _validate_log_name(name: str) -> str:
    """Aceita só nomes do whitelist; bloqueia path traversal.

    Retorna o nome canônico ou levanta HTTPException 400.
    """
    if name not in _LOG_FILES_META:
        raise HTTPException(
            400,
            f"Arquivo '{name}' não é um log canônico. "
            f"Aceitos: {list(_LOG_FILES_META.keys())}",
        )
    return name


def _resolve_log_path(name: str) -> Path:
    """Resolve <LOG_DIR>/<name>.log com path safety adicional (defense in depth)."""
    safe_name = _validate_log_name(name)
    root = _log_dir().resolve()
    path = (root / f"{safe_name}.log").resolve()
    # Garante que path resolvido está SOB root (extra segurança vs symlink)
    if not str(path).startswith(str(root)):
        raise HTTPException(400, "Path inválido (traversal detectado).")
    return path


def _is_archive_file(name: str) -> bool:
    """True se arquivo é backup rotacionado (ex: app.log.2026-05-23 ou app.log.1).

    TimedRotatingFileHandler usa sufixo de data; RotatingFileHandler usa
    inteiros. Pegamos ambos os padrões.
    """
    parts = name.split(".")
    if len(parts) < 3:
        return False
    if parts[-2] != "log":
        return False
    # Sufixo data ISO YYYY-MM-DD ou inteiro
    suffix = parts[-1]
    if suffix.isdigit():
        return True
    try:
        datetime.strptime(suffix, "%Y-%m-%d")
        return True
    except ValueError:
        return False


async def _audit(action: str, actor_id: str, details: Optional[dict] = None) -> None:
    """Best-effort. Falha não bloqueia."""
    try:
        await audit_repo.create({
            "entity_type": "logs",
            "entity_id": "system",
            "action": action,
            "actor": actor_id,
            "details": json.dumps(details or {}),
        })
    except Exception as e:
        logger.warning(f"audit log falhou para {action}: {e}")


# ─── Endpoints ───────────────────────────────────────────────────


@router.get("/stats")
async def logs_stats(user: dict = Depends(require_user)):
    """Lista estado de cada arquivo + uso de disco da pasta.

    Returns:
      {
        log_dir, log_dir_exists, log_format,
        disk: {total, used, free, used_pct},
        files: [{name, path, exists, size_bytes, size_human, mtime,
                 line_count_approx, retention_days, description}],
        archives: [{name, size_bytes, mtime}],
        totals: {files_size, archives_size, all_size}
      }
    """
    root = _log_dir()
    files = []
    archives = []
    files_size = 0
    archives_size = 0

    for name, meta in _LOG_FILES_META.items():
        path = root / f"{name}.log"
        entry = {
            "name": name,
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": 0,
            "size_human": "—",
            "mtime": None,
            "line_count_approx": 0,
            "retention_days": meta["retention_days"],
            "description": meta["description"],
        }
        if path.exists():
            stat = path.stat()
            entry["size_bytes"] = stat.st_size
            entry["size_human"] = _human_bytes(stat.st_size)
            entry["mtime"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            # Line count approximado: divide bytes pela média (~200 B/linha JSON)
            entry["line_count_approx"] = stat.st_size // 200
            files_size += stat.st_size
        files.append(entry)

    # Lista archives (arquivos rotacionados)
    if root.exists():
        for p in sorted(root.iterdir()):
            if p.is_file() and _is_archive_file(p.name):
                stat = p.stat()
                archives.append({
                    "name": p.name,
                    "size_bytes": stat.st_size,
                    "size_human": _human_bytes(stat.st_size),
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
                archives_size += stat.st_size

    # Disk usage da pasta de logs (best-effort — usa shutil.disk_usage no dir pai)
    disk = {"total": 0, "used": 0, "free": 0, "used_pct": 0}
    try:
        target = root if root.exists() else root.parent
        usage = shutil.disk_usage(target)
        disk = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "used_pct": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
            "total_human": _human_bytes(usage.total),
            "free_human": _human_bytes(usage.free),
        }
    except Exception as e:
        logger.warning(f"shutil.disk_usage falhou: {e}")

    return {
        "log_dir": str(root.resolve()) if root.exists() else str(root),
        "log_dir_exists": root.exists(),
        "log_format": os.environ.get("LOG_FORMAT", "json"),
        "disk": disk,
        "files": files,
        "archives": archives,
        "totals": {
            "files_size": files_size,
            "files_size_human": _human_bytes(files_size),
            "archives_size": archives_size,
            "archives_size_human": _human_bytes(archives_size),
            "all_size": files_size + archives_size,
            "all_size_human": _human_bytes(files_size + archives_size),
            "archives_count": len(archives),
        },
    }


@router.get("/tail/{name}")
async def logs_tail(
    name: str,
    lines: int = Query(100, ge=1, le=_TAIL_MAX_LINES),
    user: dict = Depends(require_user),
):
    """Últimas N linhas de um arquivo. Útil pra debug ad-hoc sem Grafana.

    Limite: 1000 linhas e 5 MB de janela (defensivo).
    """
    path = _resolve_log_path(name)
    if not path.exists():
        raise HTTPException(404, f"Arquivo '{name}.log' não existe ainda.")

    size = path.stat().st_size
    # Lê os últimos N bytes (no máximo) e divide por linha
    bytes_to_read = min(size, _TAIL_MAX_BYTES)
    try:
        with open(path, "rb") as f:
            if size > bytes_to_read:
                f.seek(size - bytes_to_read)
                # Descarta primeira linha parcial (provavelmente cortada)
                f.readline()
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
    except Exception as e:
        raise HTTPException(500, f"Erro lendo arquivo: {e}")

    return {
        "file": f"{name}.log",
        "total_size_bytes": size,
        "returned_lines": len(tail),
        "truncated": size > bytes_to_read,
        "lines": tail,
    }


@router.post("/clear/{name}")
async def logs_clear(
    name: str,
    user: dict = Depends(require_user),
):
    """Trunca um arquivo de log (preserva file descriptor — handler continua
    escrevendo). Reservado a role=root.
    """
    if (user.get("role") or "").lower() != "root":
        raise HTTPException(403, "Apenas usuários root podem truncar logs.")
    path = _resolve_log_path(name)
    if not path.exists():
        raise HTTPException(404, f"Arquivo '{name}.log' não existe.")

    size_before = path.stat().st_size
    try:
        # Truncate preservando inode (handler aberto continua escrevendo)
        with open(path, "w", encoding="utf-8") as f:
            f.truncate(0)
    except Exception as e:
        raise HTTPException(500, f"Erro ao truncar: {e}")

    await _audit(
        "logs.clear",
        user.get("id", ""),
        {"file": f"{name}.log", "size_before": size_before},
    )
    return {
        "file": f"{name}.log",
        "cleared": True,
        "size_before": size_before,
        "size_after": path.stat().st_size,
    }


@router.post("/rotate")
async def logs_rotate(user: dict = Depends(require_user)):
    """Força rotação de TODOS os handlers TimedRotatingFileHandler ativos.

    Útil pra "fechar o ciclo do dia" antecipadamente quando o arquivo
    atual está crescendo demais. Cria backup com sufixo de data atual e
    reabre o arquivo vazio.

    Reservado a role=root.
    """
    if (user.get("role") or "").lower() != "root":
        raise HTTPException(403, "Apenas usuários root podem forçar rotação.")
    rotated = []
    failed = []
    root_logger = logging.getLogger()
    # Procura handlers TimedRotatingFileHandler em todos os loggers conhecidos
    handlers_seen = set()
    for h in root_logger.handlers:
        if isinstance(h, TimedRotatingFileHandler) and id(h) not in handlers_seen:
            handlers_seen.add(id(h))
            try:
                h.doRollover()
                rotated.append(getattr(h, "baseFilename", "?"))
            except Exception as e:
                failed.append({"handler": getattr(h, "baseFilename", "?"), "error": str(e)})

    await _audit(
        "logs.rotate",
        user.get("id", ""),
        {"rotated_count": len(rotated), "failed_count": len(failed)},
    )
    return {
        "rotated": rotated,
        "failed": failed,
        "rotated_count": len(rotated),
    }


@router.delete("/archives")
async def logs_delete_archives(
    older_than_days: int = Query(0, ge=0, le=365,
                                  description="Apaga só arquivos com mtime > N dias. 0=todos."),
    user: dict = Depends(require_user),
):
    """Apaga arquivos rotacionados (sufixo de data ou inteiro) preservando
    os arquivos atuais (app.log, tabular.log, etc).

    `older_than_days=0` (default) apaga todos os archives.
    Reservado a role=root.
    """
    if (user.get("role") or "").lower() != "root":
        raise HTTPException(403, "Apenas usuários root podem apagar archives.")

    root = _log_dir()
    if not root.exists():
        return {"deleted": [], "freed_bytes": 0, "freed_human": "0 B"}

    cutoff_ts = None
    if older_than_days > 0:
        import time
        cutoff_ts = time.time() - (older_than_days * 86400)

    deleted = []
    freed_bytes = 0
    for p in list(root.iterdir()):
        if not (p.is_file() and _is_archive_file(p.name)):
            continue
        if cutoff_ts is not None and p.stat().st_mtime > cutoff_ts:
            continue
        size = p.stat().st_size
        try:
            p.unlink()
            deleted.append({"name": p.name, "size_bytes": size})
            freed_bytes += size
        except OSError as e:
            logger.warning(f"Falha ao apagar {p}: {e}")

    await _audit(
        "logs.delete_archives",
        user.get("id", ""),
        {"count": len(deleted), "freed_bytes": freed_bytes,
         "older_than_days": older_than_days},
    )
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "freed_bytes": freed_bytes,
        "freed_human": _human_bytes(freed_bytes),
    }


# ─── IA-assistida: explicação de logs (Log Viewer 2.0) ───────────


class ExplainRequest(BaseModel):
    """Payload para análise das linhas selecionadas pelo LLM primário."""
    lines: list[str] = Field(..., min_length=1, max_length=500)
    question: str = Field("", max_length=1000)
    preset: str = Field("", pattern=r"^(summary|errors|anomalies|hypothesis|)$")
    file_name: str = Field("", max_length=100)


_EXPLAIN_PRESETS = {
    "summary": (
        "Resuma o que aconteceu nestas linhas em até 5 bullets curtos. "
        "Foque no fluxo: requisições recebidas, respostas, latências típicas."
    ),
    "errors": (
        "Identifique e categorize todos os erros (level=ERROR/CRITICAL ou "
        "status_code≥500 ou stack traces). Para cada categoria: quantas "
        "ocorrências, sintoma, e onde apareceu (logger/path)."
    ),
    "anomalies": (
        "Detecte padrões anormais: latências altas (duration_ms outlier), "
        "rajadas de erros, repetição suspeita do mesmo evento, gaps no "
        "timeline. Compare com a baseline da própria janela analisada."
    ),
    "hypothesis": (
        "Proponha 1-3 hipóteses de causa-raiz para os problemas observados, "
        "ordenadas por probabilidade. Para cada uma: evidência nas linhas, "
        "próximo passo de verificação."
    ),
}


@router.post("/explain")
async def logs_explain(
    payload: ExplainRequest,
    user: dict = Depends(require_user),
):
    """Pede ao LLM primário uma análise semântica das linhas de log.

    Usa o provider/modelo configurado em `settings.primary_provider/primary_model`
    (UI: /settings → Plataforma → Modelo Primário). Fallback: `default_llm_provider`
    (azure). Quando nenhum estiver configurado, devolve 503.

    O backend é stateless — recebe as linhas já filtradas pelo frontend e
    monta o prompt. Sem persistência da resposta (operador copia se quiser).
    """
    # Import lazy: evita carregar langchain quando a feature não é usada
    from app.core.config import get_settings
    from app.core.llm_providers import get_provider

    settings = get_settings()
    provider_name = (settings.primary_provider or settings.default_llm_provider or "azure").strip()
    model = (settings.primary_model or "").strip() or None

    system_prompt = (
        "Você é um SRE/observability engineer experiente analisando logs de "
        "aplicação Python (FastAPI). Os logs seguem o schema estruturado JSON: "
        "ts, level, logger, msg, event, request_id, trace_id, user_id, method, "
        "path, status_code, duration_ms.\n\n"
        "Diretrizes:\n"
        "- Responda em português brasileiro, em markdown.\n"
        "- Seja objetivo: nada de preâmbulos ou disclaimers genéricos.\n"
        "- Cite linhas específicas pelo timestamp (`ts`) quando relevante.\n"
        "- Se não houver evidência para uma conclusão, diga 'não há evidência'.\n"
        "- Não invente. Não generalize além do que as linhas mostram."
    )

    parts: list[str] = []
    if payload.preset and payload.preset in _EXPLAIN_PRESETS:
        parts.append(_EXPLAIN_PRESETS[payload.preset])
    if payload.question.strip():
        parts.append(f"Pergunta do operador: {payload.question.strip()}")
    if not parts:
        parts.append(_EXPLAIN_PRESETS["summary"])

    parts.append(
        f"\n## Linhas analisadas ({len(payload.lines)}"
        f"{' de ' + payload.file_name if payload.file_name else ''}):"
    )
    parts.append("```")
    parts.extend(payload.lines)
    parts.append("```")

    user_prompt = "\n\n".join(parts)

    try:
        if model:
            provider = get_provider(provider_name, model=model)
        else:
            provider = get_provider(provider_name)
    except Exception as e:
        raise HTTPException(503, f"Provider '{provider_name}' não disponível: {e}")

    try:
        result = await provider.generate(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as e:
        logger.error("logs.explain falhou", exc_info=True)
        raise HTTPException(502, f"Erro ao chamar LLM ({provider_name}): {e}")

    await _audit(
        "logs.explain",
        user.get("id", ""),
        {
            "file": payload.file_name,
            "lines_count": len(payload.lines),
            "preset": payload.preset,
            "has_question": bool(payload.question.strip()),
            "provider": provider_name,
            "model": result.get("model"),
        },
    )

    return {
        "answer": result.get("content", ""),
        "model": result.get("model") or model or provider_name,
        "provider": provider_name,
        "lines_analyzed": len(payload.lines),
        "usage": result.get("usage", {}),
    }


# ─── Helpers ──────────────────────────────────────────────────────


def _human_bytes(n: int) -> str:
    """Formata bytes em KB/MB/GB humano."""
    if n is None or n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.1f} {units[i]}"
