"""Platform IO public exports, loaded only when requested."""

from importlib import import_module

_EXPORTS = {
    "AdapterIdentity": ("adapter_policy", "AdapterIdentity"),
    "AdapterPolicyManager": ("adapter_policy", "AdapterPolicyManager"),
    "AdapterPolicyResult": ("adapter_policy", "AdapterPolicyResult"),
    "get_adapter_policy_manager": ("adapter_policy", "get_adapter_policy_manager"),
    "PlatformIOManager": ("manager", "PlatformIOManager"),
    "get_platform_io_manager": ("manager", "get_platform_io_manager"),
    "RouteKeyFactory": ("route_key_factory", "RouteKeyFactory"),
    "RouteTable": ("routing", "RouteTable"),
    "DeliveryBatch": ("types", "DeliveryBatch"),
    "DeliveryReceipt": ("types", "DeliveryReceipt"),
    "DeliveryStatus": ("types", "DeliveryStatus"),
    "DriverDescriptor": ("types", "DriverDescriptor"),
    "DriverKind": ("types", "DriverKind"),
    "InboundMessageEnvelope": ("types", "InboundMessageEnvelope"),
    "RouteBinding": ("types", "RouteBinding"),
    "RouteKey": ("types", "RouteKey"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module("." + module, __name__), symbol)
    globals()[name] = value
    return value
