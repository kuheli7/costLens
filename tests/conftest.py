import os

import pytest


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch):
    """Tests must never reach a real AWS account, even if credentials exist on the machine."""
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    assert os.environ["AWS_ACCESS_KEY_ID"] == "testing"
