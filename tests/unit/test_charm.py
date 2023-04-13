# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest.mock as mock
from pathlib import Path

import lightkube.codecs as codecs
import ops.testing
import pytest
import yaml
from lightkube import ApiError
from ops.manifests import ManifestClientError
from ops.model import BlockedStatus, MaintenanceStatus, WaitingStatus

from charm import AzureCloudProviderCharm

ops.testing.SIMULATE_CAN_CONNECT = True


@pytest.fixture
def harness():
    harness = ops.testing.Harness(AzureCloudProviderCharm)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture(autouse=True)
def mock_ca_cert(tmpdir):
    ca_cert = Path(tmpdir) / "ca.crt"
    with mock.patch.object(AzureCloudProviderCharm, "CA_CERT_PATH", ca_cert):
        yield ca_cert


@pytest.fixture()
def control_plane(harness):
    rel_id = harness.add_relation("external-cloud-provider", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/1")


@pytest.fixture()
def integrator():
    with mock.patch("charm.AzureIntegrationRequires") as mocked:
        integrator = mocked.return_value
        integrator.tenant_id = "0000000-0000-0000-0000-000000000000"
        integrator.aad_client = "0000000-0000-0000-0000-000000000000"
        integrator.aad_client_secret = "0000000-0000-0000-0000-000000000000"
        integrator.subscription_id = "0000000-0000-0000-0000-000000000000"
        integrator.resource_group = "name"
        integrator.resource_group_location = "eastus"
        integrator.subnet_name = "subnet"
        integrator.security_group_name = "subnet-group"
        integrator.vnet_name = "vnet-name"
        integrator.vnet_resource_group = "vnet-resource-group"
        integrator.evaluate_relation.return_value = None
        yield integrator


@pytest.fixture()
def certificates():
    with mock.patch("charm.CertificatesRequires") as mocked:
        certificates = mocked.return_value
        certificates.ca = "abcd"
        certificates.evaluate_relation.return_value = None
        yield certificates


@pytest.fixture()
def kube_control():
    with mock.patch("charm.KubeControlRequirer") as mocked:
        kube_control = mocked.return_value
        kube_control.evaluate_relation.return_value = None
        kube_control.get_registry_location.return_value = "rocks.canonical.com/cdk"
        kube_control.get_cluster_tag.return_value = "kubernetes-thing"
        kube_control.get_controller_taints.return_value = []
        kube_control.get_controller_labels.return_value = []
        kube_control.relation.app.name = "kubernetes-control-plane"
        kube_control.relation.units = [f"kubernetes-control-plane/{_}" for _ in range(2)]
        yield kube_control


def test_waits_for_integrator(harness):
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required azure-integration relation"


@pytest.mark.usefixtures("integrator")
def test_waits_for_certificates(harness):
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required certificates"

    # Test adding the certificates relation
    rel_cls = type(charm.certificates)
    rel_cls.relation = property(rel_cls.relation.func)
    rel_cls._data = property(rel_cls._data.func)
    rel_id = harness.add_relation("certificates", "easyrsa")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for certificates"
    harness.add_relation_unit(rel_id, "easyrsa/0")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for certificates"
    harness.update_relation_data(
        rel_id,
        "easyrsa/0",
        yaml.safe_load(Path("tests/data/certificates_data.yaml").read_text()),
    )
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required kube-control relation"


@mock.patch("ops.interface_kube_control.KubeControlRequirer.create_kubeconfig")
@pytest.mark.usefixtures("integrator", "certificates")
def test_waits_for_kube_control(mock_create_kubeconfig, harness):
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required kube-control relation"

    # Add the kube-control relation
    rel_cls = type(charm.kube_control)
    rel_cls.relation = property(rel_cls.relation.func)
    rel_cls._data = property(rel_cls._data.func)
    rel_id = harness.add_relation("kube-control", "kubernetes-control-plane")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for kube-control relation"

    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for kube-control relation"
    mock_create_kubeconfig.assert_not_called()

    harness.update_relation_data(
        rel_id,
        "kubernetes-control-plane/0",
        yaml.safe_load(Path("tests/data/kube_control_data.yaml").read_text()),
    )
    mock_create_kubeconfig.assert_has_calls(
        [
            mock.call(charm.CA_CERT_PATH, "/root/.kube/config", "root", charm.unit.name),
            mock.call(charm.CA_CERT_PATH, "/home/ubuntu/.kube/config", "ubuntu", charm.unit.name),
        ]
    )
    assert isinstance(charm.unit.status, MaintenanceStatus)
    assert charm.unit.status.message == "Deploying Azure Cloud Provider"


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
def test_waits_for_config(harness, lk_client, caplog):
    harness.begin_with_initial_hooks()

    lk_client.list.return_value = [mock.Mock(**{"metadata.annotations": {}})]
    caplog.clear()
    harness.update_config(
        {
            "control-node-selector": "gcp.io/my-control-node=",
        }
    )
    provider_messages = {r.message for r in caplog.records if "provider" in r.filename}

    assert provider_messages == {
        "Adding provider tolerations from control-plane",
        "Adding provider topologySpreadConstraints",
        'Applying provider Control Node Selector as gcp.io/my-control-node: ""',
        "Replacing default cluster-name to kubernetes-thing",
        "Applying provider secret data",
        "Setting wait-routes=false",
    }

    caplog.clear()
    harness.update_config(
        {
            "control-node-selector": "",
            "image-registry": "dockerhub.io",
        }
    )
    provider_messages = {r.message for r in caplog.records if "provider" in r.filename}

    assert provider_messages == {
        "Adding provider tolerations from control-plane",
        "Adding provider topologySpreadConstraints",
        'Applying provider Control Node Selector as juju-application: "kubernetes-control-plane"',
        "Replacing default cluster-name to kubernetes-thing",
        "Applying provider secret data",
        "Setting wait-routes=false",
    }


@pytest.fixture()
def mock_get_response(lk_client, api_error_klass):
    def client_get_response(obj_type, name, *, namespace=None, labels=None):
        try:
            return codecs.from_dict(
                dict(
                    apiVersion="v1",
                    kind=obj_type.__name__,
                    metadata=dict(name=name, namespace=namespace, labels=labels),
                )
            )
        except AttributeError:
            raise api_error_klass()

    with mock.patch.object(lk_client, "get", side_effect=client_get_response):
        yield client_get_response


@pytest.fixture()
def mock_list_response(lk_client, mock_get_response):
    def client_list_response(obj_type, *, namespace=None, labels=None):
        try:
            return [
                mock_get_response(obj_type, name="MockThing", namespace=namespace, labels=labels)
            ]
        except ApiError:
            return []

    with mock.patch.object(lk_client, "list", side_effect=client_list_response):
        yield client_list_response


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_list_resources(harness, caplog):
    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {}
    harness.charm._list_resources(event)
    (results,), _ = event.set_results.call_args
    correct, extra, missing = (
        results.get(f"cloud-provider-azure-{_}") for _ in ["correct", "extra", "missing"]
    )
    assert correct and len(correct.splitlines()) == 3
    assert missing and len(missing.splitlines()) == 8
    assert extra and len(extra.splitlines()) == 2


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_list_resources_filtered(harness, caplog):
    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {"resources": "Secret Banana", "controller": "cloud-provider-azure"}
    harness.charm._list_resources(event)
    (results,), _ = event.set_results.call_args
    correct, extra, missing = (
        results.get(f"cloud-provider-azure-{_}") for _ in ["correct", "extra", "missing"]
    )
    assert missing is None
    assert correct and len(correct.splitlines()) == 1
    assert extra and len(extra.splitlines()) == 1


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_scrub_resources(harness, lk_client, mock_get_response, caplog):
    class Secret:
        pass

    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {"resources": "Secret", "controller": "cloud-provider-azure"}
    with mock.patch.object(lk_client, "delete") as mock_delete:
        harness.charm._scrub_resources(event)
    expected = mock_get_response(Secret, "MockThing", namespace="kube-system")
    mock_delete.assert_called_with(
        type(expected), expected.metadata.name, namespace=expected.metadata.namespace
    )


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
def test_action_sync_resources(harness, lk_client, mock_get_response, caplog):
    class MockApiError(ApiError):
        def __init__(self):
            pass

    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {"resources": "Secret", "controller": "cloud-provider-azure"}
    with mock.patch.object(lk_client, "list", return_value=[]):
        with mock.patch.object(lk_client, "get", side_effect=MockApiError):
            with mock.patch.object(lk_client, "apply") as mock_apply:
                harness.charm._sync_resources(event)
    args, kwargs = mock_apply.call_args
    (resource,) = args
    assert all(
        [
            resource.kind == "Secret",
            resource.metadata.name == "azure-cloud-config",
            resource.metadata.namespace == "kube-system",
        ]
    ) and kwargs == {"force": True}, "Failed to create secret"


def test_install_or_upgrade_apierror(harness, lk_client: mock.MagicMock):
    harness.begin_with_initial_hooks()
    with mock.patch.object(lk_client, "apply", side_effect=ManifestClientError("foo")):
        charm = harness.charm
        charm.stored.config_hash = "mock_hash"
        mock_event = mock.MagicMock()
        charm._install_or_upgrade(mock_event)
        mock_event.defer.assert_called_once()
        assert isinstance(charm.unit.status, WaitingStatus)


def test_cleanup_apierror(harness, lk_client: mock.MagicMock):
    harness.begin_with_initial_hooks()
    with mock.patch.object(lk_client, "delete", side_effect=ManifestClientError("foo")):
        charm = harness.charm
        charm.stored.config_hash = "mock_hash"
        mock_event = mock.MagicMock()
        charm._cleanup(mock_event)
        mock_event.defer.assert_called_once()
        assert isinstance(charm.unit.status, WaitingStatus)


@pytest.mark.parametrize(
    "side_effect,message",
    [
        pytest.param(
            ManifestClientError("foo"),
            "Failed to apply missing resources. API Server unavailable.",
            id="API Unavailable",
        )
    ],
)
def test_sync_resources_message(harness, lk_client: mock.MagicMock, side_effect, message):
    with mock.patch.object(lk_client, "list", side_effect=side_effect):
        with mock.patch.object(lk_client, "apply", side_effect=side_effect):
            harness.begin_with_initial_hooks()
            charm = harness.charm
            mock_event = mock.MagicMock()
            charm._sync_resources(mock_event)
            mock_event.set_results.assert_called_once_with({"result": message})
