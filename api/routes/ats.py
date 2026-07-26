"""Read-only inventory for versioned ATS adapter capabilities."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from submitters.platforms import registered_adapters

router = APIRouter(tags=["ats-adapters"])


class AdapterCapabilityResponse(BaseModel):
    ats: str
    adapter_version: str
    selector_version: str
    execution_contract_version: str | None
    transport: str
    authentication_mode: str
    supported_controls: list[str] = Field(default_factory=list)
    qualification_tier: str
    qualified_form_scope: list[str] = Field(default_factory=list)
    final_execution_enabled: bool


@router.get("/ats/adapters", response_model=list[AdapterCapabilityResponse])
async def list_ats_adapters() -> list[AdapterCapabilityResponse]:
    """Expose the bounded registry used by inspection and final execution."""
    return [
        AdapterCapabilityResponse(
            ats=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            execution_contract_version=descriptor.execution_contract_version,
            transport=descriptor.transport,
            authentication_mode=descriptor.authentication_mode,
            supported_controls=list(descriptor.supported_controls),
            qualification_tier=descriptor.qualification.value,
            qualified_form_scope=list(descriptor.qualified_form_scope),
            final_execution_enabled=descriptor.allows_final_execution,
        )
        for descriptor in registered_adapters()
    ]
