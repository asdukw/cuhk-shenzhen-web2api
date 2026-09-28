"""Offline lifecycle checks for the native Node Steel manager."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cuhk_shenzhen_web2api import local_steel


class ManagedLocalSteelTests(unittest.TestCase):
    def test_starts_and_stops_node_stack(self) -> None:
        stack = local_steel.NodeStack(api=object(), executor=object())  # type: ignore[arg-type]
        with (
            patch.object(local_steel, "start_node_stack", return_value=stack) as start,
            patch.object(local_steel, "stop_node_stack") as stop,
        ):
            with local_steel.managed_local_steel():
                pass
            start.assert_called_once_with()
            stop.assert_called_once_with(stack)

    def test_start_failure_does_not_stop_anything(self) -> None:
        with (
            patch.object(
                local_steel, "start_node_stack", side_effect=RuntimeError("not ready")
            ),
            self.assertRaisesRegex(RuntimeError, "not ready"),
            local_steel.managed_local_steel(),
        ):
            pass

    def test_reports_not_ready_when_services_are_down(self) -> None:
        with (
            patch.object(local_steel, "resolve_settings") as resolve,
            patch.object(
                local_steel,
                "_read_pid_file",
                return_value={"api": None, "executor": None},
            ),
            patch.object(local_steel, "_process_alive", return_value=False),
            patch.object(local_steel, "_ready", return_value=False),
        ):
            resolve.return_value.executor_url = "http://127.0.0.1:3003"
            result = local_steel.status()
            self.assertFalse(result["api_ready"])
            self.assertFalse(result["executor_ready"])


if __name__ == "__main__":
    unittest.main()
