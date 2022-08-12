# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import ipaddress
import logging
import shlex
from pathlib import Path

import pytest
from lightkube import AsyncClient
from lightkube.codecs import from_dict
from lightkube.resources.core_v1 import Node

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    charm = next(Path(".").glob("azure-cloud-provider*.charm"), None)
    if not charm:
        log.info("Build Charm...")
        charm = await ops_test.build_charm(".")

    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
    ]

    bundle, *overlays = await ops_test.async_render_bundles(*overlays, charm=charm.resolve())

    log.info("Deploy Charm...")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} " + " ".join(
        f"--overlay={f} --trust" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"Bundle deploy failed: {(stderr or stdout).strip()}"

    log.info(stdout)
    await ops_test.model.block_until(
        lambda: "azure-cloud-provider" in ops_test.model.applications, timeout=60
    )

    await ops_test.model.wait_for_idle(wait_for_active=True, timeout=60 * 60)


async def test_provider_ids(kubernetes):
    async for node in kubernetes.list(Node):
        assert node.spec.providerID.startswith("azure://")


@pytest.fixture
async def pod_with_volume(kubernetes, ops_test):
    name = ops_test.model_name
    pvc = from_dict(
        dict(
            kind="PersistentVolumeClaim",
            apiVersion="v1",
            metadata=dict(name=name, labels=dict(claim_pvc="pvc")),
            spec=dict(
                accessModes=["ReadWriteOnce"],
                resources=dict(requests=dict(storage="10Mi")),
                storageClassName="csi-azure-default",
            ),
        )
    )
    busybox = from_dict(
        dict(
            kind="Pod",
            apiVersion="v1",
            metadata=dict(name=name, labels=dict(claim_pvc="pod")),
            spec=dict(
                containers=[
                    dict(
                        image="busybox",
                        command=["sleep", "3600"],
                        imagePullPolicy="IfNotPresent",
                        name="busybox",
                        volumeMounts=[dict(mountPath="/pv", name="testvolume")],
                    )
                ],
                restartPolicy="Always",
                volumes=[dict(name="testvolume", persistentVolumeClaim=dict(claimName=name))],
            ),
        )
    )
    await asyncio.gather(
        *[kubernetes.create(rsc, namespace=rsc.metadata.namespace) for rsc in [pvc, busybox]]
    )
    yield busybox
    await asyncio.gather(
        *[
            kubernetes.delete(type(rsc), name=name, namespace=rsc.metadata.namespace)
            for rsc in [pvc, busybox]
        ]
    )


async def test_create_persistent_volume(kubernetes, pod_with_volume):
    _fix = pod_with_volume
    res, name, namespace = type(_fix), _fix.metadata.name, _fix.metadata.namespace
    try:
        pod = await asyncio.wait_for(
            kubernetes.wait(res, name, namespace=namespace, for_conditions=["ContainersReady"]),
            timeout=30.0,
        )
    except asyncio.TimeoutError as e:
        raise AssertionError("Timeout waiting for pod to be ready") from e
    assert pod.status.phase == "Running"


@pytest.fixture
async def loadbalanced_service(kubernetes, ops_test):
    name = ops_test.model_name
    deployment = from_dict(
        dict(
            kind="Deployment",
            apiVersion="apps/v1",
            metadata=dict(name=name, labels=dict(app=name)),
            spec=dict(
                selector=dict(matchLabels=dict(app=name)),
                template=dict(
                    metadata=dict(labels=dict(app=name)),
                    spec=dict(
                        containers=[
                            dict(
                                image="gcr.io/google-samples/node-hello:1.0",
                                name="node-hello",
                                ports=[dict(containerPort=8080, protocol="TCP")],
                            )
                        ]
                    ),
                ),
            ),
        )
    )
    service = from_dict(
        dict(
            kind="Service",
            apiVersion="v1",
            metadata=dict(name=name, labels=dict(hello_svc="svc")),
            spec=dict(
                type="LoadBalancer",
                selector=dict(app=name),
                ports=[dict(port=8080, protocol="TCP", targetPort=8080)],
            ),
        )
    )

    await asyncio.gather(
        *[
            kubernetes.create(rsc, namespace=rsc.metadata.namespace)
            for rsc in [deployment, service]
        ]
    )
    yield deployment, service
    await asyncio.gather(
        *[
            kubernetes.delete(type(rsc), name=name, namespace=rsc.metadata.namespace)
            for rsc in [deployment, service]
        ]
    )


async def test_create_service_loadbalancer(kubernetes: AsyncClient, loadbalanced_service):
    _dep, _svc = loadbalanced_service

    res, name, namespace = type(_dep), _dep.metadata.name, _dep.metadata.namespace
    try:
        deployment = await asyncio.wait_for(
            kubernetes.wait(res, name, namespace=namespace, for_conditions=["Available"]),
            timeout=30.0,
        )
    except asyncio.TimeoutError as e:
        raise AssertionError("Timeout waiting for deployment to be ready") from e

    # confirm deployment
    assert deployment.status.availableReplicas == 1

    res, name, namespace = type(_svc), _svc.metadata.name, _svc.metadata.namespace

    async def get_and_check():
        while True:
            service = await kubernetes.get(res, name, namespace=namespace)
            if not service.status.loadBalancer.ingress:
                log.info("Loadbalancer service not yet ready.")
                await asyncio.sleep(5)
                continue

            (svc_ip,) = map(lambda ingress: ingress.ip, service.status.loadBalancer.ingress)
            try:
                ipaddress.ip_address(svc_ip)
                break
            except ValueError:
                log.info(f"Loadbalancer service ip wasn't an IP address {svc_ip}.")
                pass
            await asyncio.sleep(5)

    # confirm loadbalancer goes active
    try:
        await asyncio.wait_for(get_and_check(), timeout=30.0)
    except asyncio.TimeoutError as e:
        raise AssertionError("Timeout waiting for service to be ready") from e
