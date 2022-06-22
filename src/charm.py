#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the azure CPI operator charm."""

import logging
from pathlib import Path
from typing import Optional

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    Relation,
    WaitingStatus,
)
from ops.manifests import Collector

from config import CharmConfig
from provider_manifests import AzureProviderManifests
from requires_azure_integration import AzureIntegrationRequires
from requires_certificates import CertificatesRequires
from requires_kube_control import KubeControlRequires

log = logging.getLogger(__name__)


class AzureCloudProviderCharm(CharmBase):
    """Dispatch logic for the AzureCloudProvider charm."""

    CA_CERT_PATH = Path("/srv/kubernetes/ca.crt")

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        # Relation Validator and datastore
        self.integrator = AzureIntegrationRequires(self)
        self.kube_control = KubeControlRequires(self)
        self.certificates = CertificatesRequires(self)
        # Config Validator and datastore
        self.charm_config = CharmConfig(self)

        self.CA_CERT_PATH.parent.mkdir(exist_ok=True)
        self.stored.set_default(
            cluster_tag=None,  # passing along to the integrator from the kube-control relation
            config_hash=None,  # hashed value of the provider config once valid
            deployed=False,  # True if the config has been applied after new hash
        )
        self.collector = Collector(
            AzureProviderManifests(
                self.charm_config,
                self.integrator,
                self.control_plane_relation,
                self.kube_control,
            )
        )

        self.framework.observe(self.on.kube_control_relation_created, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_joined, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_changed, self._cluster_tag)
        self.framework.observe(self.on.kube_control_relation_broken, self._merge_config)

        self.framework.observe(self.on.certificates_relation_created, self._merge_config)
        self.framework.observe(self.on.certificates_relation_changed, self._merge_config)
        self.framework.observe(self.on.certificates_relation_broken, self._merge_config)

        self.framework.observe(self.on.external_cloud_provider_relation_joined, self._merge_config)
        self.framework.observe(self.on.external_cloud_provider_relation_broken, self._merge_config)

        self.framework.observe(self.on.azure_integration_relation_joined, self._merge_config)
        self.framework.observe(self.on.azure_integration_relation_changed, self._merge_config)
        self.framework.observe(self.on.azure_integration_relation_broken, self._merge_config)

        self.framework.observe(self.on.list_versions_action, self._list_versions)
        self.framework.observe(self.on.list_resources_action, self._list_resources)
        self.framework.observe(self.on.scrub_resources_action, self._scrub_resources)
        self.framework.observe(self.on.update_status, self._update_status)

        self.framework.observe(self.on.install, self._install_or_upgrade)
        self.framework.observe(self.on.upgrade_charm, self._install_or_upgrade)
        self.framework.observe(self.on.config_changed, self._merge_config)
        self.framework.observe(self.on.stop, self._cleanup)

    def _list_versions(self, event):
        self.collector.list_versions(event)

    def _list_resources(self, event):
        manifests = event.params.get("controller", "")
        resources = event.params.get("resources", "")
        return self.collector.list_resources(event, manifests, resources)

    def _scrub_resources(self, event):
        manifests = event.params.get("controller", "")
        resources = event.params.get("resources", "")
        return self.collector.scrub_resources(event, manifests, resources)

    def _update_status(self, _):
        if not self.stored.deployed:
            return

        unready = self.collector.unready
        if unready:
            self.unit.status = WaitingStatus(", ".join(unready))
        else:
            self.unit.status = ActiveStatus("Ready")
            self.unit.set_workload_version(self.collector.short_version)
            self.app.status = ActiveStatus(self.collector.long_version)

    @property
    def control_plane_relation(self) -> Optional[Relation]:
        """Find a control-plane-node external-cloud-provider relation."""
        return self.model.get_relation("external-cloud-provider")

    def _kube_control(self, event=None):
        self.kube_control.set_auth_request(self.unit.name)
        return self._merge_config(event)

    def _cluster_tag(self, event=None):
        cluster_tag = self.kube_control.cluster_tag
        if self.stored.cluster_tag != cluster_tag:
            log.info(f"Updating cluster-tag to {cluster_tag}")
            self.stored.cluster_tag = cluster_tag
            self.integrator.tag_instance({"k8s-io-cluster-name": cluster_tag})
        return self._merge_config(event)

    def _check_kube_control(self, event):
        self.unit.status = MaintenanceStatus("Evaluating kubernetes authentication.")
        evaluation = self.kube_control.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = WaitingStatus(evaluation)
            else:
                self.unit.status = BlockedStatus(evaluation)
            return False
        if not self.kube_control.get_auth_credentials(self.unit.name):
            self.unit.status = WaitingStatus("Waiting for kube-control: unit credentials")
            return False
        self.kube_control.create_kubeconfig(
            self.CA_CERT_PATH, "/root/.kube/config", "root", self.unit.name
        )
        self.kube_control.create_kubeconfig(
            self.CA_CERT_PATH, "/home/ubuntu/.kube/config", "ubuntu", self.unit.name
        )
        return True

    def _check_certificates(self, event):
        self.unit.status = MaintenanceStatus("Evaluating certificates.")
        evaluation = self.certificates.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = WaitingStatus(evaluation)
            else:
                self.unit.status = BlockedStatus(evaluation)
            return False
        self.CA_CERT_PATH.write_text(self.certificates.ca)
        return True

    def _check_azure_relation(self, event):
        self.unit.status = MaintenanceStatus("Evaluating azure.")
        evaluation = self.integrator.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = WaitingStatus(evaluation)
            else:
                self.unit.status = BlockedStatus(evaluation)
            return False
        return True

    def _check_config(self):
        self.unit.status = MaintenanceStatus("Evaluating charm config.")
        evaluation = self.charm_config.evaluate()
        if evaluation:
            self.unit.status = BlockedStatus(evaluation)
            return False
        return True

    def _merge_config(self, event=None):
        if not self._check_azure_relation(event):
            return

        if not self._check_certificates(event):
            return

        if not self._check_kube_control(event):
            return

        if not self._check_config():
            return

        self.unit.status = MaintenanceStatus("Evaluating Manifests")
        new_hash = 0
        for controller in self.collector.manifests.values():
            evaluation = controller.evaluate()
            if evaluation:
                self.unit.status = BlockedStatus(evaluation)
                return
            new_hash += controller.hash()

        if new_hash == self.stored.config_hash:
            return

        self.stored.config_hash = new_hash
        self.stored.deployed = False
        self._install_or_upgrade()

    def _install_or_upgrade(self, _event=None):
        if not self.stored.config_hash:
            return
        self.unit.status = MaintenanceStatus("Deploying Azure Cloud Provider")
        self.unit.set_workload_version("")
        for controller in self.collector.manifests.values():
            controller.apply_manifests()
        self.stored.deployed = True

    def _cleanup(self, _event):
        if self.stored.config_hash:
            self.unit.status = MaintenanceStatus("Cleaning up Azure Cloud Provider")
            for controller in self.collector.manifests.values():
                controller.delete_manifests(ignore_unauthorized=True)
        self.unit.status = MaintenanceStatus("Shutting down")


if __name__ == "__main__":
    main(AzureCloudProviderCharm)
