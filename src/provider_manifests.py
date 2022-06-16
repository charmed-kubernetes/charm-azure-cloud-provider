# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of azure cloud provider specific details of the kubernetes manifests."""
import json
import logging
from hashlib import md5
from typing import Dict, List, Optional

import humps

from manifests import (
    CharmLabel,
    ConfigRegistry,
    Manifests,
    Patch,
    Toleration,
    update_toleration,
)

log = logging.getLogger(__file__)
SECRET_NAME = "azure-cloud-config"


class UpdateSecret(Patch):
    """Update the secret as a patch since the manifests includes a default."""

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

    def __call__(self, obj):
        """Update the secrets object in the deployment."""
        if not (obj.kind == "Secret" and obj.metadata.name == SECRET_NAME):
            return

        if any(s is self.manifests.config.get(s) for s in self.REQUIRED):
            log.error("secret data item is None")
            return

        log.info("Applying provider secret data")
        azure_json = json.loads(obj.stringData["azure.json"])
        required = {k: v for k, v in self.manifests.config.items() if k in self.REQUIRED}
        optional = {k: v for k, v in self.manifests.config.items() if k in self.OPTIONAL and v}

        azure_json.update(**humps.camelize(required))  # updated required
        for key in self.OPTIONAL:  # remove optional keys
            azure_json.pop(humps.camelize(key), None)
        azure_json.update(**humps.camelize(optional))  # set any available optional keys
        obj.stringData["azure.json"] = json.dumps(azure_json)


class UpdateNode(Patch):
    """Update the node manager daemonset as a patch."""

    NAME = "cloud-node-manager"
    REQUIRED = {
        "control-node-selector",
    }

    def _adjuster(self, toleration: Toleration) -> List[Toleration]:
        node_selector = self.manifests.config.get("control-node-selector", {})
        if toleration.key and toleration.key.startswith("node-role.kubernetes.io"):
            return [
                Toleration(
                    key=key,
                    value=value,
                    effect=toleration.effect,
                    operator=toleration.operator,
                    tolerationSeconds=toleration.tolerationSeconds,
                )
                for key, value in node_selector.items()
            ]
        return [toleration]

    def __call__(self, obj):
        """Update the DaemonSet object in the cloud-node-manager."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == self.NAME):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"provider control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return

        update_toleration(obj, self._adjuster)

        container = next(
            filter(lambda c: c.name == self.NAME, obj.spec.template.spec.containers), None
        )
        if container:
            assert "--wait-routes" in container.command[-1]
            container.command[-1] = "--wait-routes=false"
            log.info("Setting wait-routes=false")


class UpdateController(Patch):
    """Update the cloud controller Deployment/Pod as a patch."""

    NAME = "cloud-controller-manager"
    REQUIRED = {
        "control-node-selector",
    }
    OPTIONAL = {
        "cluster-cidr",
        "route-reconciliation-period",
    }

    def _adjuster(self, toleration: Toleration) -> List[Toleration]:
        node_selector = self.manifests.config.get("control-node-selector", {})
        return [
            Toleration(
                key=key,
                value=value,
                effect=toleration.effect,
                operator=toleration.operator,
                tolerationSeconds=toleration.tolerationSeconds,
            )
            for key, value in node_selector.items()
        ]

    def _update_args(self, spec) -> None:
        cluster_tag = self.manifests.config.get("cluster-tag")
        container = next(filter(lambda c: c.name == self.NAME, spec.containers), None)
        if container:
            arguments = dict(arg.split("=") for arg in container.args)
            arguments["--allocate-node-cidrs"] = "false"
            arguments["--configure-cloud-routes"] = "false"
            for arg in self.OPTIONAL:
                arguments.pop(f"--{arg}", None)
            if cluster_tag:
                log.info(f"Replacing default cluster-name to {cluster_tag}")
                arguments["--cluster-name"] = cluster_tag
            container.args = [f"{key}={value}" for key, value in arguments.items()]


class UpdateControllerPod(UpdateController):
    """Update the Pod object to reference juju supplied node selector."""

    def __call__(self, obj):
        """Update the Deployment object in the deployment."""
        if not (obj.kind == "Pod" and obj.metadata.name == self.NAME):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"provider control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return

        update_toleration(obj, self._adjuster)
        self._update_args(obj.spec)


class UpdateControllerDeployment(UpdateController):
    """Update the Deployment object to reference juju supplied node selector."""

    def __call__(self, obj):
        """Update the Deployment object in the deployment."""
        if not (obj.kind == "Deployment" and obj.metadata.name == self.NAME):
            return
        node_selector = self.manifests.config.get("control-node-selector")
        if not isinstance(node_selector, dict):
            log.error(
                f"provider control-node-selector was an unexpected type: {type(node_selector)}"
            )
            return
        obj.spec.template.spec.nodeSelector = node_selector
        node_selector_text = " ".join('{0}: "{1}"'.format(*t) for t in node_selector.items())
        log.info(f"Applying provider Control Node Selector as {node_selector_text}")

        replicas = self.manifests.config.get("replicas")
        if replicas and obj.spec.replicas != replicas:
            log.info(f"Replacing default replicas of {obj.spec.replicas} to {replicas}")
            obj.spec.replicas = replicas

        update_toleration(obj, self._adjuster)
        self._update_args(obj.spec.template.spec)


class AzureProviderManifests(Manifests):
    """Deployment Specific details for the azure-cloud-provider."""

    def __init__(self, charm_name, charm_config, integrator, control_plane, kube_control):
        manipulations = [
            CharmLabel(self),
            ConfigRegistry(self),
            UpdateSecret(self),
            UpdateControllerDeployment(self),
            UpdateNode(self),
        ]
        super().__init__(charm_name, "upstream/cloud_provider", manipulations=manipulations)
        self.charm_config = charm_config
        self.integrator = integrator
        self.control_plane = control_plane
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
            config["image-registry"] = self.kube_control.registry_location
            config["cluster-tag"] = self.kube_control.cluster_tag

        if self.control_plane:
            config["control-node-selector"] = {"juju-application": self.control_plane.app.name}
            config["replicas"] = len(self.control_plane.units)

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("provider-release", None)

        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(json.dumps(self.config, sort_keys=True).encode("utf8")).hexdigest(), 16)

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = UpdateSecret.REQUIRED | UpdateControllerDeployment.REQUIRED | UpdateNode.REQUIRED
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"Provider manifests waiting for definition of {prop}"
        return None
