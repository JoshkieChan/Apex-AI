"""The task project is the only project a connection check may use."""

from __future__ import annotations

import pytest

from apex.connectors.project import bind_project, execution_project, reset_project
from apex.errors import DomainError


def test_payload_cannot_supply_a_missing_project() -> None:
    token = bind_project(None)
    try:
        assert execution_project({"project_key": "alpha"}) is None
    finally:
        reset_project(token)


def test_payload_must_match_the_task_project() -> None:
    token = bind_project("alpha")
    try:
        assert execution_project({"project_key": "alpha"}) == "alpha"
        assert execution_project({}) == "alpha"
        with pytest.raises(DomainError) as caught:
            execution_project({"project_key": "beta"})
        assert caught.value.code == "project_mismatch"
    finally:
        reset_project(token)
