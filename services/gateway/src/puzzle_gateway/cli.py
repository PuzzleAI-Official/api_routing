from __future__ import annotations

import argparse
import json
from pathlib import Path

from puzzle_shared import InvoiceLineItemsMode, WorkflowProviderPriorSpec

from puzzle_gateway.db import SessionLocal, create_all
from puzzle_gateway.documents.registry_import import import_provider_registry
from puzzle_gateway.documents.validation import (
    choose_sample_file,
    list_provider_service_statuses,
    validate_provider_service,
)
from puzzle_gateway.documents.workflow_priors import (
    INVOICE_WORKFLOW,
    list_workflow_priors,
    seed_invoice_workflow_priors,
    upsert_workflow_prior,
    workflow_provider_service_statuses,
)
from puzzle_gateway.seed import seed_default_tenant
from puzzle_gateway.storage import get_object_store

DEFAULT_DOCUMENT_PROVIDERS = "mindee,veryfi,nanonets,klippa"
PHASE2_SERVICE_IDS = {
    "mindee": "model_inference",
    "veryfi": "documents",
    "nanonets": "ocr_model",
    "klippa": "generic",
}


def _provider_names(raw: str) -> list[str]:
    aliases = {"verify": "veryfi", "Klippa": "klippa"}
    return [
        aliases.get(name.strip(), name.strip().lower())
        for name in raw.split(",")
        if name.strip()
    ]


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _run_command(args: argparse.Namespace) -> bool:  # noqa: PLR0911
    if args.command == "import-provider-registry":
        with SessionLocal() as session:
            imported = import_provider_registry(
                session,
                tenant_id=args.tenant_id,
                path=args.path,
                providers=_provider_names(args.providers),
            )
            session.commit()
            print(f"imported_providers={','.join(imported)}")
        return True
    if args.command == "provider-capabilities":
        with SessionLocal() as session:
            _print_json(
                [
                    item.model_dump(mode="json")
                    for item in list_provider_service_statuses(session, tenant_id=args.tenant_id)
                ]
            )
        return True
    if args.command == "validate-provider-service":
        with SessionLocal() as session:
            result = validate_provider_service(
                session,
                tenant_id=args.tenant_id,
                provider_id=args.provider,
                service_id=args.service,
                sample_path=Path(args.sample_path),
                object_store=get_object_store(),
                write=bool(args.write),
                check_async=bool(args.check_async),
                actor="provider-validation-cli",
            )
            session.commit() if args.write else session.rollback()
            _print_json(result.to_safe_dict())
        return True
    if args.command == "validate-document-providers":
        sample_path = choose_sample_file(Path(args.sample_dir))
        results = []
        with SessionLocal() as session:
            for provider in _provider_names(args.providers):
                service_id = PHASE2_SERVICE_IDS[provider]
                result = validate_provider_service(
                    session,
                    tenant_id=args.tenant_id,
                    provider_id=provider,
                    service_id=service_id,
                    sample_path=sample_path,
                    object_store=get_object_store(),
                    write=bool(args.write),
                    check_async=bool(args.check_async),
                    actor="provider-validation-cli",
                )
                results.append(result.to_safe_dict())
            session.commit() if args.write else session.rollback()
            _print_json(results)
        return True
    if args.command == "seed-invoice-workflow-priors":
        with SessionLocal() as session:
            rows = seed_invoice_workflow_priors(session, tenant_id=args.tenant_id)
            session.commit()
            _print_json(
                [
                    {
                        "workflow": row.workflow,
                        "provider_id": row.provider_id,
                        "service_id": row.service_id,
                        "active": row.active,
                        "quality_prior": row.quality_prior,
                        "fallback_priority": row.fallback_priority,
                    }
                    for row in rows
                ]
            )
        return True
    if args.command == "workflow-priors":
        with SessionLocal() as session:
            _print_json(
                [
                    item.model_dump(mode="json")
                    for item in list_workflow_priors(
                        session,
                        tenant_id=args.tenant_id,
                        workflow=args.workflow,
                    )
                ]
            )
        return True
    if args.command == "upsert-workflow-prior":
        metadata = json.loads(args.metadata or "{}")
        if not isinstance(metadata, dict):
            raise ValueError("--metadata must be a JSON object")
        with SessionLocal() as session:
            row = upsert_workflow_prior(
                session,
                tenant_id=args.tenant_id,
                spec=WorkflowProviderPriorSpec(
                    workflow=args.workflow,
                    provider_id=args.provider,
                    service_id=args.service,
                    active=not bool(args.inactive),
                    quality_prior=args.quality_prior,
                    fallback_priority=args.fallback_priority,
                    notes=args.notes,
                    metadata=metadata,
                ),
            )
            session.commit()
            _print_json(
                {
                    "workflow": row.workflow,
                    "provider_id": row.provider_id,
                    "service_id": row.service_id,
                    "active": row.active,
                    "quality_prior": row.quality_prior,
                    "fallback_priority": row.fallback_priority,
                }
            )
        return True
    if args.command == "workflow-provider-services":
        with SessionLocal() as session:
            _print_json(
                [
                    item.model_dump(mode="json")
                    for item in workflow_provider_service_statuses(
                        session,
                        tenant_id=args.tenant_id,
                        workflow=args.workflow,
                        provider_set_name=args.provider_set,
                        line_items_mode=InvoiceLineItemsMode(args.line_items_mode),
                    )
                ]
            )
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="puzzle-gateway")
    subcommands = parser.add_subparsers(dest="command")
    import_registry = subcommands.add_parser("import-provider-registry")
    import_registry.add_argument("--tenant-id", required=True)
    import_registry.add_argument("--path", required=True)
    import_registry.add_argument("--providers", default=DEFAULT_DOCUMENT_PROVIDERS)

    capabilities = subcommands.add_parser("provider-capabilities")
    capabilities.add_argument("--tenant-id", required=True)

    validate_one = subcommands.add_parser("validate-provider-service")
    validate_one.add_argument("--tenant-id", required=True)
    validate_one.add_argument("--provider", required=True)
    validate_one.add_argument("--service", required=True)
    validate_one.add_argument("--sample-path", required=True)
    validate_one.add_argument("--write", action="store_true")
    validate_one.add_argument("--check-async", action="store_true")

    validate_many = subcommands.add_parser("validate-document-providers")
    validate_many.add_argument("--tenant-id", required=True)
    validate_many.add_argument("--sample-dir", required=True)
    validate_many.add_argument("--providers", default=DEFAULT_DOCUMENT_PROVIDERS)
    validate_many.add_argument("--write", action="store_true")
    validate_many.add_argument("--check-async", action="store_true")

    seed_priors = subcommands.add_parser("seed-invoice-workflow-priors")
    seed_priors.add_argument("--tenant-id", required=True)

    workflow_priors = subcommands.add_parser("workflow-priors")
    workflow_priors.add_argument("--tenant-id", required=True)
    workflow_priors.add_argument("--workflow", default=INVOICE_WORKFLOW)

    upsert_prior = subcommands.add_parser("upsert-workflow-prior")
    upsert_prior.add_argument("--tenant-id", required=True)
    upsert_prior.add_argument("--workflow", default=INVOICE_WORKFLOW)
    upsert_prior.add_argument("--provider", required=True)
    upsert_prior.add_argument("--service", required=True)
    upsert_prior.add_argument("--quality-prior", type=float, required=True)
    upsert_prior.add_argument("--fallback-priority", type=int, default=100)
    upsert_prior.add_argument("--inactive", action="store_true")
    upsert_prior.add_argument("--notes")
    upsert_prior.add_argument("--metadata", default="{}")

    workflow_status = subcommands.add_parser("workflow-provider-services")
    workflow_status.add_argument("--tenant-id", required=True)
    workflow_status.add_argument("--workflow", default=INVOICE_WORKFLOW)
    workflow_status.add_argument("--provider-set", default="documents-default")
    workflow_status.add_argument(
        "--line-items-mode",
        choices=[item.value for item in InvoiceLineItemsMode],
        default=InvoiceLineItemsMode.PREFERRED.value,
    )
    args = parser.parse_args()

    create_all()
    if _run_command(args):
        return

    with SessionLocal() as session:
        tenant, api_key = seed_default_tenant(session)
        session.commit()
        print(f"tenant_id={tenant.id}")
        print(f"api_key={api_key}")


if __name__ == "__main__":
    main()
