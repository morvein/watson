from . import amazon, facebook, instagram, microsoft, odnoklassniki, snapchat, zoho

ADAPTERS = (instagram.adapter, amazon.adapter, microsoft.adapter, facebook.adapter, odnoklassniki.adapter,
            zoho.adapter, snapchat.adapter)
SERVICE_ALIASES = {"ok": "odnoklassniki", "ok.ru": "odnoklassniki", "одноклассники": "odnoklassniki"}


def select_services(names=None):
    if names is None:
        return [adapter for adapter in ADAPTERS if adapter is not zoho.adapter]
    names = {name.strip().lower() for name in names}
    names = {SERVICE_ALIASES.get(name, name) for name in names}
    if names - {item.name for item in ADAPTERS}:
        raise ValueError("Unknown service. List available services with: watson methods")
    return [item for item in ADAPTERS if item.name in names]
