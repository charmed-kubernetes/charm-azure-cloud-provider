# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of azure cloud provider specific details of the kubernetes manifests."""
import json
import logging
import pickle
from hashlib import md5
from typing import Dict, Optional

import humps
from lightkube.models.core_v1 import Toleration, TopologySpreadConstraint
from lightkube.models.meta_v1 import LabelSelector
from ops.interface_azure.requires import AzureIntegrationRequires
from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch

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

    def __call__(self, obj):
        """Update the DaemonSet object in the cloud-node-manager."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == self.NAME):
            return

        current_keys = {toleration.key for toleration in obj.spec.template.spec.tolerations}
        missing_tolerations = [
            Toleration(
                key=taint.key,
                value=taint.value,
                effect=taint.effect,
            )
            for taint in self.manifests.config.get("control-node-taints", [])
            if taint.key not in current_keys
        ]
        obj.spec.template.spec.tolerations += missing_tolerations
        log.info("Adding provider tolerations from control-plane")

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

        current_keys = {toleration.key for toleration in spec.tolerations}
        missing_tolerations = [
            Toleration(
                key=taint.key,
                value=taint.value,
                effect=taint.effect,
            )
            for taint in self.manifests.config.get("control-node-taints", [])
            if taint.key not in current_keys
        ]
        spec.tolerations += missing_tolerations
        log.info("Adding provider tolerations from control-plane")


class UpdateControllerPod(UpdateController):
    """Update the Pod object to reference juju supplied node selector."""

    def __call__(self, obj):
        """Update the Deployment object in the deployment."""
        if not (obj.kind == "Pod" and obj.metadata.name == self.NAME):
            return

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

        # to prevent replicas from landing on the same nodes, use topologySpreadConstraints
        # https://github.com/kubernetes-sigs/cloud-provider-azure/blob/fe6f72141d63a21525b96873f83e7a1c3dbae39e/helm/cloud-provider-azure/templates/cloud-provider-azure.yaml#L170-L177
        log.info("Adding provider topologySpreadConstraints")

        obj.spec.template.spec.topologySpreadConstraints = [
            TopologySpreadConstraint(
                maxSkew=1,
                topologyKey="kubernetes.io/hostname",
                whenUnsatisfiable="DoNotSchedule",
                labelSelector=LabelSelector(matchLabels=dict(**obj.spec.selector.matchLabels)),
            )
        ]
        self._update_args(obj.spec.template.spec)


class AzureProviderManifests(Manifests):
    """Deployment Specific details for the azure-cloud-provider."""

    def __init__(self, charm, charm_config, integrator, kube_control):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
            UpdateSecret(self),
            UpdateControllerPod(self),  # v1.1.4 v1.23.0 create Pods
            UpdateControllerDeployment(self),  # v1.24.0 creates a Deployment
            UpdateNode(self),
        ]
        super().__init__(
            "cloud-provider-azure", charm.model, "upstream/cloud_provider", manipulations
        )
        self.charm_config = charm_config
        self.integrator: AzureIntegrationRequires = integrator
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
                    "aad-client-id": self.integrator.aad_client_id,
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
            config["cluster-tag"] = self.kube_control.get_cluster_tag()
            config["control-node-taints"] = self.kube_control.get_controller_taints() or [
                Toleration("NoSchedule", "node-role.kubernetes.io/control-plane")
            ]  # by default
            config["control-node-selector"] = {
                label.key: label.value for label in self.kube_control.get_controller_labels()
            } or {"juju-application": self.kube_control.relation.app.name}
            config["replicas"] = len(self.kube_control.relation.units)

        config.update(**self.charm_config.available_data)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("provider-release", None)

        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = UpdateSecret.REQUIRED | UpdateControllerDeployment.REQUIRED
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"Provider manifests waiting for definition of {prop}"
        return None
