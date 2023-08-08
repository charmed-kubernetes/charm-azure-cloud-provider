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

from backports.cached_property import cached_property
from ops.charm import RelationBrokenEvent
from ops.framework import Object
from pydantic import BaseModel, Extra, Field, StrictStr, ValidationError


class AzureIntegrationData(BaseModel, extra=Extra.allow):
    """Requires side of azure-integration:client relation."""

    resource_group_location: StrictStr = Field(alias="resource-group-location")
    vnet_name: StrictStr = Field(alias="vnet-name")
    vnet_resource_group: StrictStr = Field(alias="vnet-resource-group")
    subnet_name: StrictStr = Field(alias="subnet-name")
    security_group_name: StrictStr = Field(alias="security-group-name")
    security_group_resource_group: StrictStr = Field(alias="security-group-resource-group")
    aad_client: StrictStr = Field(alias="aad-client")
    aad_client_secret: StrictStr = Field(alias="aad-client-secret")
    tenant_id: StrictStr = Field(alias="tenant-id")
    use_managed_identity: bool = Field(alias="use-managed-identity")


log = logging.getLogger(__name__)


# block size to read data from Azure metadata service
# (realistically, just needs to be bigger than ~20 chars)
READ_BLOCK_SIZE = 2048


class AzureIntegrationRequires(Object):
    """Requires side of azure-integration:client relation."""

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
    def _raw_data(self):
        if self.relation and self.relation.units:
            return self.relation.data[list(self.relation.units)[0]]
        return None

    @cached_property
    def _data(self) -> Optional[AzureIntegrationData]:
        raw = self._raw_data
        return AzureIntegrationData(**raw) if raw else None

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
            self._data
        except ValidationError as ve:
            log.error(f"{self.endpoint} relation data not yet valid. ({ve}")
            return False
        if self._data is None:
            log.error(f"{self.endpoint} relation data not yet available.")
            return False
        return True

    @cached_property
    def vm_metadata(self):
        """Metadata about this VM instance."""
        req = Request(self._metadata_url, headers=self._metadata_headers)
        with urlopen(req) as fd:
            metadata = fd.read(READ_BLOCK_SIZE).decode("utf8").strip()
            return json.loads(metadata)

    @property
    def resource_group_location(self) -> Optional[str]:
        """The resource-group-location value."""
        if not self.is_ready:
            return None
        return self._data.resource_group_location

    @property
    def vnet_name(self) -> Optional[str]:
        """The vnet-name value."""
        if not self.is_ready:
            return None
        return self._data.vnet_name

    @property
    def vnet_resource_group(self) -> Optional[str]:
        """The vnet-resource-group value."""
        if not self.is_ready:
            return None
        return self._data.vnet_resource_group

    @property
    def subnet_name(self) -> Optional[str]:
        """The subnet-name value."""
        if not self.is_ready:
            return None
        return self._data.subnet_name

    @property
    def security_group_name(self) -> Optional[str]:
        """The security-group-name value."""
        if not self.is_ready:
            return None
        return self._data.security_group_name

    @property
    def security_group_resource_group(self) -> Optional[str]:
        """The security-group-resource-group value."""
        if not self.is_ready:
            return None
        return self._data.security_group_resource_group

    @property
    def use_managed_identity(self) -> Optional[bool]:
        """The use-managed-identity value."""
        if not self.is_ready:
            return None
        return self._data.use_managed_identity

    @property
    def aad_client(self) -> Optional[str]:
        """The aad-client value."""
        if not self.is_ready:
            return None
        return self._data.aad_client

    @property
    def aad_client_secret(self) -> Optional[str]:
        """The aad-client-secret value."""
        if not self.is_ready:
            return None
        return self._data.aad_client_secret

    @property
    def tenant_id(self) -> Optional[str]:
        """The tenant-id value."""
        if not self.is_ready:
            return None
        return self._data.tenant_id

    @property
    def vm_id(self) -> str:
        """This unit's instance ID."""
        return self.vm_metadata["compute"]["vmId"]

    @property
    def vm_name(self) -> str:
        """This unit's instance name."""
        return self.vm_metadata["compute"]["name"]

    @property
    def vm_location(self) -> str:
        """The location (region) the instance is running in."""
        return self.vm_metadata["compute"]["location"]

    @property
    def resource_group(self) -> str:
        """The resource group this unit is in."""
        return self.vm_metadata["compute"]["resourceGroupName"]

    @property
    def subscription_id(self) -> str:
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
