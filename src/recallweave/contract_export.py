from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .contract import CONTRACT_SCHEMA_VERSION, build_contract_document
from .contract_markdown import render_contract_markdown
from .contract_spec import TaskSpec
from .safe_write import install, prepare_destination, verify_destination


def _base_receipt(
    document: dict[str, Any], output_format: str
) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "operation": "export_contract",
        "output": None,
        "format": output_format,
        "task_id": document["task"]["id"],
        "retrieved_context_items": len(document["retrieved_context"]),
        "connections": len(document["connections"]),
        "constraints": len(document["constraints"]),
        "prior_decisions": len(document["prior_decisions"]),
        "acceptance_criteria": len(document["acceptance_criteria"]),
        "exclusions_enforced": document["exclusions"]["enforced"],
        "characters_used": document["budget"]["characters_used"],
        "character_budget": document["budget"]["character_budget"],
        "truncated": document["budget"]["truncated"],
        "profile": document["disclosure"]["profile"],
        "includes_passage_text": document["disclosure"]["includes_passage_text"],
        "includes_candidate_edges": document["disclosure"]["includes_candidate_edges"],
        "replacement_mode": None,
        "replacement_backup": None,
        "network_calls": 0,
        "vault_writes": 0,
    }


def export_contract(
    database: Path,
    spec: TaskSpec,
    output: Path | None,
    *,
    output_format: str = "json",
    force: bool = False,
    vault: Path | None = None,
) -> dict[str, Any]:
    if output_format not in ("json", "markdown"):
        raise ValueError(
            f"Unsupported output format {output_format!r}; expected 'json' or 'markdown'."
        )
    database = database.expanduser().resolve()
    document = build_contract_document(database, spec)

    if output is None:
        receipt = _base_receipt(document, output_format)
        if output_format == "json":
            receipt["contract"] = document
        else:
            receipt["markdown"] = render_contract_markdown(document)
        return receipt

    output = Path(os.path.abspath(output.expanduser()))
    # An artifact written INSIDE the vault is a write to the vault, and both
    # this receipt and the embedded document assert `vault_writes: 0`. Reporting
    # 1 instead would leave the artifact itself carrying a false claim -- it is
    # serialized before it is written, so it cannot describe its own
    # destination. Refusing keeps every existing claim true, and matches how
    # `index` already treats an in-vault database.
    #
    # It is also a footgun in its own right: a contract quoting the vault,
    # stored in the vault, is re-indexed on the next run and starts quoting
    # itself.
    if vault is not None:
        resolved_vault = Path(os.path.abspath(vault.expanduser()))
        if resolved_vault in output.parents or output == resolved_vault:
            raise ValueError(
                "Refusing to write a contract artifact inside the vault "
                f"({output}). The receipt and the document both assert "
                "vault_writes: 0, which would be false. Choose --output "
                "outside the vault."
            )
    guard = prepare_destination(output, database, force=force, label="Contract output")
    verify_destination(output, database, guard, label="Contract output")

    if output_format == "json":
        body = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    else:
        body = render_contract_markdown(document)

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    replacement_backup: str | None = None
    try:
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        verify_destination(output, database, guard, label="Contract output")
        replacement_backup = install(temporary, output, guard, label="Contract output")
    finally:
        temporary.unlink(missing_ok=True)

    receipt = _base_receipt(document, output_format)
    receipt["output"] = str(output)
    receipt["replacement_mode"] = (
        "two_phase_recoverable" if guard["output_existed"] else "non_replacing"
    )
    receipt["replacement_backup"] = replacement_backup
    return receipt
