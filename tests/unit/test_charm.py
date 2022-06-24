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
from ops.model import BlockedStatus, WaitingStatus

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
    with mock.patch("charm.KubeControlRequires") as mocked:
        kube_control = mocked.return_value
        kube_control.evaluate_relation.return_value = None
        kube_control.registry_location = "rocks.canonical.com/cdk"
        kube_control.cluster_tag = "kubernetes-thing"
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


@mock.patch("requires_kube_control.KubeControlRequires.create_kubeconfig")
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
    assert isinstance(charm.unit.status, BlockedStatus)
    assert (
        charm.unit.status.message
        == "Provider manifests waiting for definition of control-node-selector"
    )


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
def test_waits_for_config(harness, lk_client, caplog):
    harness.begin_with_initial_hooks()

    lk_client().list.return_value = [mock.Mock(**{"metadata.annotations": {}})]
    caplog.clear()
    harness.update_config(
        {
            "control-node-selector": 'gcp.io/my-control-node=""',
        }
    )
    provider_messages = [r.message for r in caplog.records if "provider" in r.filename]

    assert provider_messages == [
        'Applying provider Control Node Selector as gcp.io/my-control-node: ""',
        "Replacing default cluster-name to kubernetes-thing",
        "Applying provider secret data",
        "Setting wait-routes=false",
    ]

    caplog.clear()
    harness.update_config(
        {
            "control-node-selector": "",
            "image-registry": "dockerhub.io",
        }
    )
    provider_messages = [r.message for r in caplog.records if "provider" in r.filename]

    assert provider_messages == [
        'Applying provider Control Node Selector as juju-application: "kubernetes-control-plane"',
        "Replacing default cluster-name to kubernetes-thing",
        "Applying provider secret data",
        "Setting wait-routes=false",
    ]


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

    lk_client().get.side_effect = client_get_response
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

    lk_client().list.side_effect = client_list_response
    yield client_list_response


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_list_resources(harness, caplog):
    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {}
    correct, extra, missing = harness.charm._list_resources(event)
    assert len(correct) == 0
    assert len(missing) == 3
    assert len(extra) == 2
    expected_result = {
        "extra": "\n".join(sorted(str(_) for _ in extra)),
        "missing": "\n".join(sorted(str(_) for _ in missing)),
    }
    event.set_results.assert_called_once_with(expected_result)


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_list_resources_filtered(harness, caplog):
    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {"resources": "Secret Banana", "controller": "provider"}
    correct, extra, missing = harness.charm._list_resources(event)
    assert len(correct) == 0
    assert len(missing) == 1
    assert len(extra) == 1
    expected_result = {
        "extra": "\n".join(sorted(str(_) for _ in extra)),
        "missing": "\n".join(sorted(str(_) for _ in missing)),
    }
    event.set_results.assert_called_once_with(expected_result)


@pytest.mark.usefixtures("integrator", "certificates", "kube_control", "control_plane")
@pytest.mark.usefixtures("mock_list_response")
def test_action_scrub_resources(harness, lk_client, mock_get_response, caplog):
    class Secret:
        pass

    harness.begin_with_initial_hooks()
    event = mock.MagicMock()
    event.params = {"resources": "Secret", "controller": "provider"}
    harness.charm._scrub_resources(event)
    expected = mock_get_response(Secret, "MockThing", namespace="kube-system")
    lk_client().delete.assert_called_with(
        type(expected), expected.metadata.name, namespace=expected.metadata.namespace
    )
