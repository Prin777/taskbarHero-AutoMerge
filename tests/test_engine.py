from __future__ import annotations

import unittest

from tbh_automation.engine import (
    AutomationError,
    Category,
    ControlGate,
    RoundCancelled,
    RunSchedule,
    SynthesisEngine,
)


class FakeDriver:
    def __init__(self, readiness: list[bool]) -> None:
        self.readiness = iter(readiness)
        self.actions: list[str] = []

    def check_boss_portal(self) -> bool:
        self.actions.append("portal-check")
        return False

    def recover_server_error(self) -> bool:
        return False

    def open_chests(self) -> None:
        self.actions.append("space")

    def prepare_interface(self) -> None:
        self.actions.append("prepare")

    def checkpoint(self) -> None:
        self.actions.append("checkpoint")

    def select_category(self, category: Category) -> None:
        self.actions.append(f"select:{category.value}")

    def click_auto_fill(self) -> None:
        self.actions.append("fill")

    def combine_button_is_enabled(self) -> bool:
        value = next(self.readiness)
        self.actions.append(f"ready:{value}")
        return value

    def click_combine(self) -> None:
        self.actions.append("combine")

    def wait_for_combination(self) -> None:
        self.actions.append("wait-combine")

    def click_return_items(self) -> None:
        self.actions.append("return")


class SynthesisEngineTests(unittest.TestCase):
    def test_portal_default_off_but_space_runs_once_before_fill(self):
        driver=FakeDriver([False]*4)
        SynthesisEngine(driver).run_round()
        self.assertNotIn('portal-check',driver.actions)
        self.assertEqual(driver.actions.count('space'),1)
        self.assertLess(driver.actions.index('prepare'),driver.actions.index('space'))
        self.assertLess(driver.actions.index('space'),driver.actions.index('fill'))

    def test_failed_server_attempt_still_counts_toward_click_safety_limit(self):
        from unittest.mock import Mock
        driver=FakeDriver([True,True])
        driver.recover_server_error=Mock(side_effect=[True,False])
        with self.assertRaisesRegex(AutomationError,"安全上限 2"):
            SynthesisEngine(driver,max_combinations_per_category=2).run_round()
        self.assertEqual(driver.actions.count("combine"),2)

    def test_server_error_returns_items_and_refills_without_counting_failed_attempt(self):
        from unittest.mock import Mock
        driver=FakeDriver([True,True,False,False,False,False])
        driver.recover_server_error=Mock(side_effect=[True,False])
        result=SynthesisEngine(driver).run_round()
        self.assertEqual(result.total,1)
        first=driver.actions.index("wait-combine")
        self.assertEqual(driver.actions[first+1:first+5],["checkpoint","return","checkpoint","fill"])

    def test_repeated_server_errors_pause_at_three(self):
        from unittest.mock import Mock
        driver=FakeDriver([True]*3)
        driver.recover_server_error=Mock(return_value=True)
        with self.assertRaisesRegex(AutomationError,"3"):
            SynthesisEngine(driver).run_round()
        self.assertEqual(driver.actions.count("combine"),3)

    def test_portal_is_checked_once_after_prepare_and_space(self):
        driver = FakeDriver([False]*8)
        engine = SynthesisEngine(driver,portal_enabled=lambda:True)
        engine.run_round()
        engine.run_round()
        self.assertEqual(driver.actions.count("portal-check"), 2)
        for index, action in enumerate(driver.actions):
            if action == "portal-check":
                self.assertEqual(driver.actions[index-4:index], ["prepare","checkpoint","space","checkpoint"])

    def test_cancelled_portal_check_never_starts_synthesis(self):
        from unittest.mock import Mock
        driver = FakeDriver([])
        driver.check_boss_portal = Mock(side_effect=RoundCancelled)
        with self.assertRaises(RoundCancelled):
            SynthesisEngine(driver,portal_enabled=lambda:True).run_round()
        self.assertNotIn("fill",driver.actions)

    def test_blue_button_combines_and_only_equipment_and_material_are_selected(self) -> None:
        driver = FakeDriver([True, False, False, False, False])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        self.assertEqual(result.material_combinations, 0)
        self.assertEqual(driver.actions.count("prepare"), 1)
        self.assertEqual(
            [action for action in driver.actions if action.startswith("select:")],
            ["select:装备", "select:材料"],
        )
        self.assertNotIn("select:饰品", driver.actions)
        self.assertEqual(driver.actions.count("combine"), 1)
        combine_index = driver.actions.index("combine")
        next_fill_index = driver.actions.index("fill", combine_index)
        self.assertLess(combine_index, next_fill_index)
        self.assertEqual(driver.actions[next_fill_index + 1], "ready:False")
        self.assertFalse(any(action.startswith("observe:") for action in driver.actions))

    def test_first_grey_then_retry_blue_combines(self) -> None:
        driver = FakeDriver([False, True, False, False, False, False])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        first_check = driver.actions.index("ready:False")
        retry_check = driver.actions.index("ready:True")
        between = driver.actions[first_check:retry_check]
        self.assertIn("return", between)
        self.assertIn("fill", between)

    def test_two_grey_attempts_end_category_and_return_residue(self) -> None:
        driver = FakeDriver([False, False, False, False])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.total, 0)
        # 轮首清理一次；两个类别各失败两次，并各退回两次。
        self.assertEqual(driver.actions.count("return"), 5)
        self.assertEqual(driver.actions.count("fill"), 4)

    def test_grey_combine_button_is_never_clicked(self) -> None:
        driver = FakeDriver([False, False, False, False])

        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.total, 0)
        self.assertNotIn("combine", driver.actions)
        self.assertEqual(driver.actions.count("ready:False"), 4)

    def test_no_grid_observation_is_requested(self) -> None:
        driver = FakeDriver([True, False, False, False, False])

        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        self.assertEqual(driver.actions.count("combine"), 1)
        self.assertFalse(hasattr(driver, "observe_filled_cells"))

    def test_safety_limit_stops_a_possible_infinite_loop(self) -> None:
        driver = FakeDriver([True, True])
        engine = SynthesisEngine(driver, max_combinations_per_category=2)
        with self.assertRaises(AutomationError):
            engine.run_round()

class ControlGateTests(unittest.TestCase):
    def test_pause_cancels_current_generation(self) -> None:
        gate = ControlGate()
        generation = gate.generation
        gate.pause(cancel_round=True)
        with self.assertRaises(RoundCancelled):
            gate.wait_enabled(generation, 0)

    def test_toggle_can_resume(self) -> None:
        gate = ControlGate()
        self.assertFalse(gate.toggle())
        self.assertTrue(gate.toggle())
        gate.wait_enabled(gate.generation, 0)

    def test_restart_cancels_old_round_and_keeps_automation_enabled(self) -> None:
        gate = ControlGate()
        old_generation = gate.generation

        gate.restart()

        self.assertTrue(gate.enabled)
        self.assertGreater(gate.generation, old_generation)
        with self.assertRaises(RoundCancelled):
            gate.wait_enabled(old_generation, 0)
        gate.wait_enabled(gate.generation, 0)


class RunScheduleTests(unittest.TestCase):
    def test_startup_is_immediately_due(self) -> None:
        schedule = RunSchedule.starting_now(18_000, 100.0)
        self.assertTrue(schedule.is_due(100.0))

    def test_next_run_is_five_hours_after_successful_completion(self) -> None:
        schedule = RunSchedule.starting_now(18_000, 100.0)
        schedule.complete_round(250.0)
        self.assertEqual(schedule.next_due, 18_250.0)
        self.assertFalse(schedule.is_due(18_249.9))
        self.assertTrue(schedule.is_due(18_250.0))

    def test_changed_interval_recalculates_from_last_completed_round(self) -> None:
        schedule = RunSchedule.starting_now(18_000, 100.0)
        schedule.complete_round(250.0)

        schedule.change_interval(3_600)

        self.assertEqual(schedule.interval_seconds, 3_600)
        self.assertEqual(schedule.next_due, 3_850.0)

    def test_changed_interval_does_not_postpone_initial_run(self) -> None:
        schedule = RunSchedule.starting_now(18_000, 100.0)

        schedule.change_interval(3_600)

        self.assertEqual(schedule.next_due, 100.0)


if __name__ == "__main__":
    unittest.main()
