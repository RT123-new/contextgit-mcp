"""Token usage ledger.

Every context compilation appends one JSONL row recording how many tokens the
compiled patch cost versus what sending the full history would have cost. The
ledger is what powers `contextgit stats` and the `context_stats` MCP tool, so
users can see the signed net token impact, including calls where the patch cost
more than the full history.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UsageLedger:
    def __init__(self, path: str):
        self.path = path

    def record_compilation(
        self,
        *,
        conversation_id: str,
        patch_tokens: int,
        full_history_tokens: int,
        budget: int,
        selected_count: int,
        excluded_count: int,
        token_source: str,
        kind: str = "prepare_context",
    ) -> Dict[str, Any]:
        row = {
            "ts": _utc_now_iso(),
            "kind": kind,
            "conversation_id": conversation_id,
            "patch_tokens": int(patch_tokens),
            "full_history_tokens": int(full_history_tokens),
            "saved_tokens": int(full_history_tokens) - int(patch_tokens),
            "net_saved_tokens": int(full_history_tokens) - int(patch_tokens),
            "budget": int(budget),
            "selected_count": int(selected_count),
            "excluded_count": int(excluded_count),
            "token_source": token_source,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        return row

    def rows(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def summary(self, last_n_days: Optional[int] = None) -> Dict[str, Any]:
        rows = self.rows()
        if last_n_days is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - last_n_days * 86400
            kept = []
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    kept.append(row)
            rows = kept

        compilations = [r for r in rows if r.get("kind") == "prepare_context"]
        patch_total = sum(r.get("patch_tokens", 0) for r in compilations)
        full_total = sum(r.get("full_history_tokens", 0) for r in compilations)
        saved_total = sum(r.get("saved_tokens", 0) for r in compilations)
        gross_saved_total = sum(max(0, r.get("saved_tokens", 0)) for r in compilations)
        loss_total = sum(min(0, r.get("saved_tokens", 0)) for r in compilations)
        by_day: Dict[str, Dict[str, int]] = {}
        for r in compilations:
            day = (r.get("ts") or "")[:10]
            bucket = by_day.setdefault(day, {"compilations": 0, "patch_tokens": 0, "saved_tokens": 0})
            bucket["compilations"] += 1
            bucket["patch_tokens"] += r.get("patch_tokens", 0)
            bucket["saved_tokens"] += r.get("saved_tokens", 0)
        return {
            "compilations": len(compilations),
            "patch_tokens_total": patch_total,
            "full_history_tokens_total": full_total,
            "saved_tokens_total": saved_total,
            "net_saved_tokens_total": saved_total,
            "gross_saved_tokens_total": gross_saved_total,
            "loss_tokens_total": loss_total,
            "savings_pct": round(100.0 * saved_total / full_total, 2) if full_total else 0.0,
            "avg_patch_tokens": round(patch_total / len(compilations), 1) if compilations else 0.0,
            "by_day": dict(sorted(by_day.items())),
        }
