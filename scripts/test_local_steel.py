"""Offline lifecycle checks for the gateway's Compose stack ownership."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cuhk_shenzhen_web2api import local_steel


class ManagedLocalSteelTests(unittest.TestCase):
    def test_starts_and_stops_new_stack(self) -> None:
        with (
            patch.object(local_steel, "_running_services", return_value=set()),
            patch.object(local_steel, "_compose") as compose,
            patch.object(local_steel, "_wait_ready") as wait_ready,
        ):
            with local_steel.managed_local_steel():
                wait_ready.assert_called_once_with()
            self.assertEqual(
                [call.args for call in compose.call_args_list],
                [("up", "-d", "--build", "--remove-orphans"), ("down",)],
            )

    def test_keeps_existing_stack_running(self) -> None:
        with (
            patch.object(
                local_steel, "_running_services", return_value={"steel", "executor"}
            ),
            patch.object(local_steel, "_compose") as compose,
            patch.object(local_steel, "_wait_ready"),
        ):
            with local_steel.managed_local_steel():
                pass
            compose.assert_not_called()

    def test_stops_only_missing_service(self) -> None:
        with (
            patch.object(local_steel, "_running_services", return_value={"steel"}),
            patch.object(local_steel, "_compose") as compose,
            patch.object(local_steel, "_wait_ready"),
        ):
            with local_steel.managed_local_steel():
                pass
            self.assertEqual(
                [call.args for call in compose.call_args_list],
                [
                    ("up", "-d", "--build", "--no-recreate"),
                    ("stop", "executor"),
                ],
            )

    def test_cleans_up_after_readiness_failure(self) -> None:
        with (
            patch.object(local_steel, "_running_services", return_value=set()),
            patch.object(local_steel, "_compose") as compose,
            patch.object(
                local_steel, "_wait_ready", side_effect=RuntimeError("not ready")
            ),
        ):
            with (
                self.assertRaisesRegex(RuntimeError, "not ready"),
                local_steel.managed_local_steel(),
            ):
                pass
            self.assertEqual(compose.call_args_list[-1].args, ("down",))


if __name__ == "__main__":
    unittest.main()
