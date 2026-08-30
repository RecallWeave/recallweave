from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .contract_export import export_contract
from .contract_spec import TaskSpec
from .index import SCHEMA_VERSION, build_index, default_database_for_vault
from .policy import IndexPolicy
from .query import connections, context_packet, doctor, path_between, resurface, stats
from .steward_assess import assess_latest
from .steward_observe import observe_registry
from .steward_propose import propose_latest
from .steward_sources import load_registry
from .steward_state import steward_state_root
from .viewer import export_viewer_graph


class _ExactArgumentParser(argparse.ArgumentParser):
    """Do not turn abbreviated safety flags into acknowledgements."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=True))


def _add_database_locator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=_path,
        help="Explicit RecallWeave database. Otherwise the external index for --vault (or CWD) is used.",
    )
    parser.add_argument(
        "--vault",
        type=_path,
        help="Vault whose default external RecallWeave index should be queried.",
    )


def _query_database(args: argparse.Namespace) -> Path:
    if args.database is not None:
        return args.database
    return default_database_for_vault(args.vault or Path.cwd())


def _parser() -> argparse.ArgumentParser:
    parser = _ExactArgumentParser(
        prog="recallweave",
        description="Local-first, evidence-cited discovery for Obsidian vaults.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser(
        "index",
        help="Build a disposable external index without changing the vault.",
    )
    index.add_argument("vault", type=_path)
    index.add_argument(
        "--database",
        type=_path,
        help="Optional explicit database path. The safe default is outside the vault.",
    )
    policy_choice = index.add_mutually_exclusive_group()
    policy_choice.add_argument(
        "--config",
        type=_path,
        help="Apply an explicit JSON indexing policy.",
    )
    policy_choice.add_argument(
        "--no-policy",
        action="store_true",
        help="Explicitly acknowledge indexing without sensitivity or path deny rules.",
    )
    index.add_argument("--candidate-threshold", type=float, default=0.16)
    index.add_argument("--max-candidates-per-note", type=int, default=8)
    index.add_argument(
        "--allow-in-vault",
        action="store_true",
        help="Deliberately permit the database inside the vault; it will count as a vault write.",
    )
    index.add_argument(
        "--force",
        action="store_true",
        help="Deliberately replace an existing non-RecallWeave destination file.",
    )

    query = subparsers.add_parser("query", help="Return bounded cited passages and nearby evidence.")
    query.add_argument("query")
    _add_database_locator(query)
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--max-characters", type=int, default=12_000)
    query.add_argument("--include-candidates", action="store_true")

    related = subparsers.add_parser(
        "connections",
        help="Explain authored, candidate, and supporting connections for one note.",
    )
    related.add_argument("note")
    _add_database_locator(related)
    related.add_argument("--verified-only", action="store_true")
    related.add_argument("--limit", type=int, default=100)

    surface = subparsers.add_parser(
        "resurface",
        help="Find older, relevant, underlinked notes worth revisiting.",
    )
    surface.add_argument("query")
    _add_database_locator(surface)
    surface.add_argument("--limit", type=int, default=6)
    surface.add_argument("--minimum-age-days", type=int, default=30)

    path = subparsers.add_parser("path", help="Show an evidence-bearing path between two notes.")
    path.add_argument("source")
    path.add_argument("target")
    _add_database_locator(path)
    path.add_argument("--include-candidates", action="store_true")
    path.add_argument("--max-hops", type=int, default=6)

    summary = subparsers.add_parser("stats", help="Show index counts, diagnostics, and freshness.")
    _add_database_locator(summary)

    health = subparsers.add_parser(
        "doctor",
        help="List unresolved links and explain why they were not trusted.",
    )
    _add_database_locator(health)
    health.add_argument("--limit", type=int, default=100)

    export_viewer = subparsers.add_parser(
        "export-viewer",
        help="Export a local graph JSON file for the RecallWeave Atlas viewer.",
    )
    export_viewer.add_argument("output", type=_path)
    _add_database_locator(export_viewer)
    export_viewer.add_argument("--verified-only", action="store_true")
    export_viewer.add_argument(
        "--include-excerpts",
        action="store_true",
        help="Include bounded note and evidence excerpts. Off by default for privacy.",
    )
    export_viewer.add_argument("--title")
    export_viewer.add_argument(
        "--vault-name",
        dest="vault_name",
        help="Optional vault label for viewer.v2 (never a filesystem path).",
    )
    export_viewer.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing viewer JSON file.",
    )

    export_contract_parser = subparsers.add_parser(
        "contract",
        help="Export a cited task-contract work packet from a task spec and the index.",
    )
    export_contract_parser.add_argument("spec", type=_path)
    _add_database_locator(export_contract_parser)
    export_contract_parser.add_argument("--output", type=_path)
    export_contract_parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Artifact format. Defaults to json.",
    )
    export_contract_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing contract artifact.",
    )

    steward_observe = subparsers.add_parser(
        "steward-observe",
        help="Detect changes in steward sources and record a change batch.",
    )
    steward_observe.add_argument(
        "sources",
        type=_path,
        help="Path to the sources registry JSON.",
    )
    steward_observe.add_argument(
        "--state-dir",
        type=_path,
        help="Explicit steward state root. Defaults to the platform state root for this registry.",
    )

    steward_assess_parser = subparsers.add_parser(
        "steward-assess",
        help=(
            "Classify observed source changes against the index "
            "(deterministic only; no vault or index writes)."
        ),
    )
    steward_assess_parser.add_argument("sources", type=_path)
    _add_database_locator(steward_assess_parser)
    steward_assess_parser.add_argument(
        "--state-dir",
        type=_path,
        dest="state_dir",
        help="Override the default steward state directory.",
    )

    steward_propose_parser = subparsers.add_parser(
        "steward-propose",
        help=(
            "Compile reviewable proposals with hash-pinned edit scripts from "
            "the latest assessment (propose_only; no vault or index writes)."
        ),
    )
    steward_propose_parser.add_argument("sources", type=_path)
    _add_database_locator(steward_propose_parser)
    steward_propose_parser.add_argument(
        "--state-dir",
        type=_path,
        dest="state_dir",
        help="Override the default steward state directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "index":
        if args.config is None and not args.no_policy:
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "error": "PolicyChoiceRequired",
                        "message": (
                            "Indexing requires an explicit policy choice. "
                            "Pass --config <policy.json> to apply exclusions, or "
                            "--no-policy only after confirming every Markdown file "
                            "in the vault is safe to index."
                        ),
                        "operation": "index",
                    },
                    ensure_ascii=True,
                ),
                file=sys.stderr,
            )
            return 2
        database = args.database or default_database_for_vault(args.vault)

        def run_index() -> dict[str, Any]:
            policy_bytes: bytes | None = None
            if args.config is not None:
                policy_bytes = args.config.read_bytes()
            policy = (
                IndexPolicy.from_bytes(policy_bytes)
                if policy_bytes is not None
                else IndexPolicy()
            )
            policy_digest = None
            if policy_bytes is not None:
                policy_digest = hashlib.sha256(policy_bytes).hexdigest()
            receipt = build_index(
                args.vault,
                database,
                policy=policy,
                minimum_candidate_score=args.candidate_threshold,
                max_candidates_per_note=args.max_candidates_per_note,
                allow_in_vault=args.allow_in_vault,
                force=args.force,
                policy_config_sha256=policy_digest,
            )
            receipt["policy_mode"] = "config" if args.config is not None else "none"
            if policy_digest is not None:
                receipt["policy_config_sha256"] = policy_digest
            return receipt

        action: Callable[[], dict[str, Any]] = run_index
    elif args.command == "steward-observe":

        def run_observe() -> dict[str, Any]:
            registry = load_registry(args.sources)
            state_root = args.state_dir or steward_state_root(args.sources)
            return observe_registry(registry, state_root)

        action = run_observe
    else:
        database = _query_database(args)
        commands: dict[str, Callable[[], dict[str, Any]]] = {
            "query": lambda: context_packet(
                database,
                args.query,
                limit=args.limit,
                max_characters=args.max_characters,
                include_candidates=args.include_candidates,
            ),
            "connections": lambda: connections(
                database,
                args.note,
                include_candidates=not args.verified_only,
                limit=args.limit,
            ),
            "resurface": lambda: resurface(
                database,
                args.query,
                limit=args.limit,
                minimum_age_days=args.minimum_age_days,
            ),
            "path": lambda: path_between(
                database,
                args.source,
                args.target,
                include_candidates=args.include_candidates,
                max_hops=args.max_hops,
            ),
            "stats": lambda: stats(database),
            "doctor": lambda: doctor(database, limit=args.limit),
            "export-viewer": lambda: export_viewer_graph(
                database,
                args.output,
                include_candidates=not args.verified_only,
                include_excerpts=args.include_excerpts,
                title=args.title,
                vault_name=args.vault_name,
                force=args.force,
            ),
            "contract": lambda: export_contract(
                database,
                TaskSpec.from_file(args.spec),
                args.output,
                output_format=args.format,
                force=args.force,
                # The same vault this command resolves its index against, so an
                # in-vault destination is refused rather than silently making
                # `vault_writes: 0` false.
                vault=args.vault or Path.cwd(),
            ),
            "steward-assess": lambda: assess_latest(
                load_registry(args.sources),
                args.state_dir or steward_state_root(args.sources),
                database,
            ),
            "steward-propose": lambda: propose_latest(
                load_registry(args.sources),
                args.state_dir or steward_state_root(args.sources),
                database,
            ),
        }
        action = commands[args.command]
    try:
        _emit(action())
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": type(error).__name__,
                    "message": str(error),
                    "operation": args.command,
                },
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
