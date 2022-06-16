# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of azure-integration interface.

This only implements the requires side, currently, since the integrator
is still using the Reactive Charm framework self.
"""
import json
import logging
import random
import string
from typing import Optional
from urllib.request import Request, urlopen

import jsonschema
from backports.cached_property import cached_property
from ops.charm import RelationBrokenEvent
from ops.framework import Object

log = logging.getLogger(__name__)


# block size to read data from Azure metadata service
# (realistically, just needs to be bigger than ~20 chars)
READ_BLOCK_SIZE = 2048


class AzureIntegrationRequires(Object):
    """Requires side of azure-integration:client relation."""

    SCHEMA_STR = [
        "resource-group-location",
        "vnet-name",
        "vnet-resource-group",
        "subnet-name",
        "security-group-name",
        "security-group-resource-group",
        "aad-client",
        "aad-client-secret",
        "tenant-id",
    ]
    SCHEMA_BOOL = [
        "use-managed-identity",
    ]
    LIMIT = 1
    SCHEMA = dict(
        type="object",
        properties=dict(
            **{k: dict(type="string") for k in SCHEMA_STR},
            **{k: dict(type="boolean") for k in SCHEMA_BOOL},
        ),
        required=SCHEMA_STR + SCHEMA_BOOL,
    )
    IGNORE_FIELDS = {
        "egress-subnets",
        "ingress-address",
        "private-address",
    }

    # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/instance-metadata-service
    _metadata_url = "http://169.254.169.254/metadata/instance?api-version=2017-12-01"  # noqa
    _metadata_headers = {"Metadata": "true"}

    def __init__(self, charm, endpoint="azure-integration"):
        super().__init__(charm, f"relation-{endpoint}")
        self.charm = charm
        self.endpoint = endpoint
        join_event = getattr(self.charm.on, f"{endpoint.replace('-','_')}_relation_joined")
        self.charm.framework.observe(join_event, self._joined)

    def _joined(self, event):
        to_publish = event.relation.data[self.charm.unit]
        to_publish["charm"] = self.model.app.name
        to_publish["vm-id"] = self.vm_id
        to_publish["vm-name"] = self.vm_name
        to_publish["res-group"] = self.resource_group
        to_publish["model-uuid"] = self.model.uuid

    @cached_property
    def relation(self):
        """The relation to the integrator, or None."""
        return self.model.get_relation(self.endpoint)

    @cached_property
    def _data(self):
        if not (self.relation and self.relation.units):
            return {}
        raw_data = self.relation.data[list(self.relation.units)[0]]
        data = {}
        for field, raw_value in raw_data.items():
            if field in self.IGNORE_FIELDS or not raw_value:
                continue
            try:
                data[field] = json.loads(raw_value)
            except json.JSONDecodeError as e:
                log.error(f"Failed to decode relation data in {field}: {e}")
        return data

    def evaluate_relation(self, event) -> Optional[str]:
        """Determine if relation is ready."""
        no_relation = not self.relation or (
            isinstance(event, RelationBrokenEvent) and event.relation is self.relation
        )
        if not self.is_ready:
            if no_relation:
                return f"Missing required {self.endpoint} relation"
            return f"Waiting for {self.endpoint} relation"
        return None

    @property
    def is_ready(self):
        """Whether the request for this instance has been completed."""
        try:
            jsonschema.validate(self._data, self.SCHEMA)
        except jsonschema.ValidationError:
            log.error(f"{self.endpoint} relation data not yet valid.")
            return False
        return True

    def _value(self, key):
        if not self._data:
            return None
        return self._data.get(key)

    @cached_property
    def vm_metadata(self):
        """Metadata about this VM instance."""
        req = Request(self._metadata_url, headers=self._metadata_headers)
        with urlopen(req) as fd:
            metadata = fd.read(READ_BLOCK_SIZE).decode("utf8").strip()
            return json.loads(metadata)

    @property
    def resource_group_location(self):
        """The resource-group-location value."""
        return self._value("resource-group-location")

    @property
    def vnet_name(self):
        """The vnet-name value."""
        return self._value("vnet-name")

    @property
    def vnet_resource_group(self):
        """The vnet-resource-group value."""
        return self._value("vnet-resource-group")

    @property
    def subnet_name(self):
        """The subnet-name value."""
        return self._value("subnet-name")

    @property
    def security_group_name(self):
        """The security-group-name value."""
        return self._value("security-group-name")

    @property
    def security_group_resource_group(self):
        """The security-group-resource-group value."""
        return self._value("security-group-resource-group")

    @property
    def use_managed_identity(self):
        """The use-managed-identity value."""
        return self._value("use-managed-identity")

    @property
    def aad_client(self):
        """The aad-client value."""
        return self._value("aad-client")

    @property
    def aad_client_secret(self):
        """The aad-client-secret value."""
        return self._value("aad-client-secret")

    @property
    def tenant_id(self):
        """The tenant-id value."""
        return self._value("tenant-id")

    @property
    def vm_id(self):
        """This unit's instance ID."""
        return self.vm_metadata["compute"]["vmId"]

    @property
    def vm_name(self):
        """This unit's instance name."""
        return self.vm_metadata["compute"]["name"]

    @property
    def vm_location(self):
        """The location (region) the instance is running in."""
        return self.vm_metadata["compute"]["location"]

    @property
    def resource_group(self):
        """The resource group this unit is in."""
        return self.vm_metadata["compute"]["resourceGroupName"]

    @property
    def subscription_id(self):
        """The ID of the Azure Subscription this unit is in."""
        return self.vm_metadata["compute"]["subscriptionId"]

    def _request(self, keyvals):
        alphabet = string.ascii_letters + string.digits
        nonce = "".join(random.choice(alphabet) for _ in range(8))
        to_publish = self.relation.data[self.charm.unit]
        to_publish.update({k: json.dumps(v) for k, v in keyvals.items()})
        to_publish["requested"] = nonce

    def tag_instance(self, tags):
        """Request that the given tags be applied to this instance.

        # Parameters
        `tags` (dict): Mapping of tags names to values.
        """
        self._request({"instance-tags": dict(tags)})

    def enable_instance_inspection(self):
        """Request the ability to inspect instances."""
        self._request({"enable-instance-inspection": True})

    def enable_network_management(self):
        """Request the ability to manage networking."""
        self._request({"enable-network-management": True})

    def enable_loadbalancer_management(self):
        """Request the ability to manage networking."""
        self._request({"enable-loadbalancer-management": True})

    def enable_security_management(self):
        """Request the ability to manage security (e.g., firewalls)."""
        self._request({"enable-security-management": True})

    def enable_block_storage_management(self):
        """Request the ability to manage block storage."""
        self._request({"enable-block-storage-management": True})

    def enable_dns_management(self):
        """Request the ability to manage DNS."""
        self._request({"enable-dns": True})

    def enable_object_storage_access(self):
        """Request the ability to access object storage."""
        self._request({"enable-object-storage-access": True})

    def enable_object_storage_management(self):
        """Request the ability to manage object storage."""
        self._request({"enable-object-storage-management": True})
