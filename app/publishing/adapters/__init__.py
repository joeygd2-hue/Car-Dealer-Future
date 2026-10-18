from app.publishing.adapters.base import AdapterError, AdapterResult, ChannelAdapter  # noqa: F401
from app.publishing.adapters.facebook_marketplace import FacebookMarketplaceManualAdapter
from app.publishing.adapters.meta_catalog import MetaCatalogAdapter
from app.publishing.adapters.sandbox import SandboxAdapter

ADAPTERS: dict[str, ChannelAdapter] = {a.key: a for a in (FacebookMarketplaceManualAdapter(), MetaCatalogAdapter(), SandboxAdapter())}


def get_adapter(key: str) -> ChannelAdapter:
    if key not in ADAPTERS:
        raise KeyError(key)
    return ADAPTERS[key]
