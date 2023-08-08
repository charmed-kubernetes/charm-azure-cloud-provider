# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from requires_azure_integration import AzureIntegrationData
from pathlib import Path
import yaml


def test_parse_relation_data():
    d = yaml.safe_load(Path("tests/data/azure_integration_data.yaml").read_text()),
    loaded = AzureIntegrationData(**d[0])
    assert loaded.aad_client == "206e5ba9-5578-4990-a74d-7f4472c675a6"
