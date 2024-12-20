# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import asyncio
import logging
import random
import string
from pathlib import Path

import pytest
from lightkube import AsyncClient, KubeConfig
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace
from pytest_operator import OpsTest

log = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def module_name(request):
    return request.module.__name__.replace("_", "-")


async def get_leader(app):
    """Find leader unit of an application.

    Args:
        app: Juju application
    Returns:
        int: index to leader unit
    """
    is_leader = await asyncio.gather(*(u.is_leader_from_status() for u in app.units))
    for idx, flag in enumerate(is_leader):
        if flag:
            return idx


@pytest.fixture()
async def kubeconfig(ops_test: OpsTest):
    for choice in ["kubernetes-control-plane", "k8s"]:
        if app := ops_test.model.applications.get(choice):
            break
    else:
        pytest.fail("No kubernetes-control-plane or k8s application found")
    leader_idx = await get_leader(app)
    leader = app.units[leader_idx]

    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    action = await leader.run_action("get-kubeconfig")
    data = await action.wait()
    retcode, kubeconfig = (data.results.get(key, {}) for key in ["return-code", "kubeconfig"])
    if retcode != 0:
        log.error("Failed to copy kubeconfig from %s (%s)", app.name, data.results)
        pytest.fail(f"Failed to copy kubeconfig from {app.name}")
    kubeconfig_path.write_text(kubeconfig)
    assert Path(kubeconfig_path).stat().st_size, "kubeconfig file is 0 bytes"
    yield kubeconfig_path


@pytest.fixture()
async def kubernetes(kubeconfig, module_name):
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = f"{module_name}-{rand_str}"
    config = KubeConfig.from_file(kubeconfig)
    client = AsyncClient(
        config=config.get(context_name=config.current_context),
        namespace=namespace,
        trust_env=False,
    )
    namespace_obj = Namespace(metadata=ObjectMeta(name=namespace))
    await client.create(namespace_obj)
    yield client
    await client.delete(Namespace, namespace)
