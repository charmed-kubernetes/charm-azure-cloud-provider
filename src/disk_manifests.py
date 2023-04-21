# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of AzureDisk specific details of the kubernetes manifests."""
import base64
import json
import logging
import pickle
from hashlib import md5
from typing import Dict, List, Optional

import humps
from lightkube.codecs import AnyResource, from_dict
from lightkube.models.core_v1 import Toleration, TopologySpreadConstraint
from lightkube.models.meta_v1 import LabelSelector
from ops.manifests import (
    Addition,
    ConfigRegistry,
    ManifestLabel,
    Manifests,
    Patch,
    update_tolerations,
)

log = logging.getLogger(__file__)
STORAGE_CLASS_NAME = "csi-azure-{type}"


class WriteSecret(Addition):
    """Write secrets for disk permissions.

    # wokeignore:rule=master
    https://github.com/kubernetes-sigs/azuredisk-csi-driver/blob/master/docs/read-from-secret.md
    """

    REQUIRED = {
        "aad-client-id",
        "aad-client-secret",
        "resource-group",
        "location",
        "subnet-name",
        "security-group-name",
        "subscription-id",
        "tenant-id",
        "vnet-name",
        "vnet-resource-group",
    }
    OPTIONAL = {
        "load-balancer-sku",
        "primary-availability-set-name",
        "primary-scale-set-name",
        "route-table-name",
        "vm-type",
    }

    def __call__(self) -> Optional[AnyResource]:
        """Create Secret for Azure secret Deployments and Daemonsets."""
        if any(s is self.manifests.config.get(s) for s in self.REQUIRED):
            log.error("azuredisk: Secret Data unavailable")
            return None

        log.info("Applying azuredisk secret data")
        azure_json = dict(
            cloud="AzurePublicCloud",
            cloudProviderBackoff=True,
            cloudProviderBackoffRetries=6,
            cloudProviderBackoffExponent=1.5,
            cloudProviderBackoffDuration=5,
            cloudProviderBackoffJitter=1,
            cloudProviderRatelimit=True,
            cloudProviderRateLimitQPS=6,
            cloudProviderRateLimitBucket=20,
            useManagedIdentityExtension=False,
            userAssignedIdentityID="",
            useInstanceMetadata=True,
            excludeMasterFromStandardLB=False,
            maximumLoadBalancerRuleCount=250,
            enableMultipleStandardLoadBalancers=False,
            tags="a=b,c=d",
        )
        required = {k: v for k, v in self.manifests.config.items() if k in self.REQUIRED}
        optional = {k: v for k, v in self.manifests.config.items() if k in self.OPTIONAL and v}
        azure_json.update(**humps.camelize(required))  # updated required
        for key in self.OPTIONAL:  # remove optional keys
            azure_json.pop(humps.camelize(key), None)
        azure_json.update(**humps.camelize(optional))  # set any available optional keys

        base64_json = base64.b64encode(json.dumps(azure_json).encode()).decode()
        secret = dict(
            apiVersion="v1",
            kind="Secret",
            metadata=dict(name="azure-cloud-provider", namespace="kube-system"),
            data={"cloud-config": base64_json},
            type="Opaque",
        )
        return from_dict(secret)


class UpdateNode(Patch):
    """Update the node daemonset as a patch."""

    NAME = "csi-azuredisk-node"
    REQUIRED = {
        "control-node-selector",
    }

    def __call__(self, obj):
        """Update the DaemonSet object in the cloud-node-manager."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == self.NAME):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"azuredisk control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return


class UpdateController(Patch):
    """Update the disk controller Deployment as a patch."""

    NAME = "csi-azuredisk-controller"
    REQUIRED = {
        "control-node-selector",
    }

    def _adjuster(self, tolerations: List[Toleration]) -> List[Toleration]:
        node_selector = self.manifests.config.get("control-node-selector", {})
        for t in tolerations:
            if "control-plane" in t.key:
                return [
                    Toleration(
                        key=key,
                        value=value,
                        effect=t.effect,
                        operator="Equal",
                        tolerationSeconds=t.tolerationSeconds,
                    )
                    for key, value in node_selector.items()
                ]
        return []


class UpdateControllerDeployment(UpdateController):
    """Update the Deployment object to reference juju supplied node selector."""

    def __call__(self, obj):
        """Update the Deployment object in the deployment."""
        if not (obj.kind == "Deployment" and obj.metadata.name == self.NAME):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"azuredisk control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return
        obj.spec.template.spec.nodeSelector = node_selector
        node_selector_text = " ".join('{0}: "{1}"'.format(*t) for t in node_selector.items())
        log.info(f"Applying azuredisk control node selector as {node_selector_text}")

        replicas = self.manifests.config.get("replicas")
        if replicas and obj.spec.replicas != replicas:
            log.info(f"Replacing azuredisk default replicas of {obj.spec.replicas} to {replicas}")
            obj.spec.replicas = replicas

        update_tolerations(obj, self._adjuster)
        log.info("Adding azuredisk topologySpreadConstraints")

        obj.spec.template.spec.topologySpreadConstraints = [
            TopologySpreadConstraint(
                maxSkew=1,
                topologyKey="kubernetes.io/hostname",
                whenUnsatisfiable="DoNotSchedule",
                labelSelector=LabelSelector(matchLabels=dict(**obj.spec.selector.matchLabels)),
            )
        ]


class CreateStorageClass(Addition):
    """Create vmware storage class."""

    def __init__(self, manifests: "Manifests", sc_type: str):
        super().__init__(manifests)
        self.type = sc_type

    def __call__(self) -> Optional[AnyResource]:
        """Craft the storage class object."""
        storage_name = STORAGE_CLASS_NAME.format(type=self.type)
        log.info(f"Creating storage class {storage_name}")
        return from_dict(
            dict(
                kind="StorageClass",
                apiVersion="storage.k8s.io/v1",
                metadata=dict(
                    name=storage_name,
                    annotations={
                        "storageclass.kubernetes.io/is-default-class": "true",
                    },
                ),
                provisioner="disk.csi.azure.com",
                parameters=dict(
                    skuName="Standard_LRS",
                ),
                reclaimPolicy="Delete",
                volumeBindingMode="WaitForFirstConsumer",
                allowVolumeExpansion=True,
            )
        )


class AzureDiskManifests(Manifests):
    """Deployment Specific details for the cs-azuredisk-driver."""

    def __init__(self, charm, charm_config, integrator, kube_control):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
            UpdateControllerDeployment(self),
            UpdateNode(self),
            WriteSecret(self),
            CreateStorageClass(self, "default"),  # creates csi-azure-default
        ]
        super().__init__("disk-driver-azure", charm.model, "upstream/azure_disk", manipulations)
        self.charm_config = charm_config
        self.integrator = integrator
        self.kube_control = kube_control

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {}
        if self.integrator.is_ready:
            config.update(
                {
                    "tenant-id": self.integrator.tenant_id,
                    "subscription-id": self.integrator.subscription_id,
                    "aad-client-id": self.integrator.aad_client,
                    "aad-client-secret": self.integrator.aad_client_secret,
                    "resource-group": self.integrator.resource_group,
                    "location": self.integrator.resource_group_location,
                    "subnet-name": self.integrator.subnet_name,
                    "security-group-name": self.integrator.security_group_name,
                    "vnet-name": self.integrator.vnet_name,
                    "vnet-resource-group": self.integrator.vnet_resource_group,
                }
            )
        if self.kube_control.is_ready:
            config["image-registry"] = self.kube_control.get_registry_location()
            config["control-node-selector"] = {
                label.key: label.value for label in self.kube_control.get_controller_labels()
            } or {"juju-application": self.kube_control.relation.app.name}
            config["replicas"] = len(self.kube_control.relation.units)

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("azuredisk-release", None)

        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = WriteSecret.REQUIRED | UpdateControllerDeployment.REQUIRED | UpdateNode.REQUIRED
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"AzureDisk manifests waiting for definition of {prop}"
        return None
