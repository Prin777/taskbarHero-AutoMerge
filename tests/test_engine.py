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
    def __init__(self, observations: list[int], readiness: list[bool] | None = None) -> None:
        self.observations = iter(observations)
        self.readiness = iter(readiness) if readiness is not None else None
        self.last_observation = 0
        self.actions: list[str] = []

    def checkpoint(self) -> None:
        self.actions.append("checkpoint")

    def select_category(self, category: Category) -> None:
        self.actions.append(f"select:{category.value}")

    def click_auto_fill(self) -> None:
        self.actions.append("fill")

    def observe_filled_cells(self) -> int:
        value = next(self.observations)
        self.last_observation = value
        self.actions.append(f"observe:{value}")
        return value

    def combine_button_is_enabled(self) -> bool:
        value = next(self.readiness) if self.readiness is not None else self.last_observation == 9
        self.actions.append(f"ready:{value}")
        return value

    def click_combine(self) -> None:
        self.actions.append("combine")

    def wait_for_combination(self) -> None:
        self.actions.append("wait-combine")

    def click_return_items(self) -> None:
        self.actions.append("return")


class SynthesisEngineTests(unittest.TestCase):
    def test_full_grid_combines_and_only_equipment_and_material_are_selected(self) -> None:
        driver = FakeDriver([9, 3, 2, 1, 0])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        self.assertEqual(result.material_combinations, 0)
        self.assertEqual(
            [action for action in driver.actions if action.startswith("select:")],
            ["select:装备", "select:材料"],
        )
        self.assertNotIn("select:饰品", driver.actions)
        self.assertEqual(driver.actions.count("combine"), 1)
        combine_index = driver.actions.index("combine")
        next_fill_index = driver.actions.index("fill", combine_index)
        next_observe_index = next(
            index
            for index in range(next_fill_index + 1, len(driver.actions))
            if driver.actions[index].startswith("observe:")
        )
        self.assertLess(combine_index, next_fill_index)
        self.assertLess(next_fill_index, next_observe_index)

    def test_first_underfull_then_retry_full_combines(self) -> None:
        driver = FakeDriver([4, 9, 0, 0, 0, 0])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        first_observe = driver.actions.index("observe:4")
        retry_observe = driver.actions.index("observe:9")
        between = driver.actions[first_observe:retry_observe]
        self.assertIn("return", between)
        self.assertIn("fill", between)

    def test_two_underfull_attempts_end_category_and_return_residue(self) -> None:
        driver = FakeDriver([5, 5, 2, 2])
        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.total, 0)
        # 轮首清理一次；两个类别各失败两次，并各退回两次。
        self.assertEqual(driver.actions.count("return"), 5)
        self.assertEqual(driver.actions.count("fill"), 4)

    def test_false_full_grid_never_clicks_a_grey_combine_button(self) -> None:
        driver = FakeDriver(
            [9, 9, 9, 9],
            readiness=[False, False, False, False],
        )

        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.total, 0)
        self.assertNotIn("combine", driver.actions)
        self.assertEqual(driver.actions.count("ready:False"), 4)

    def test_blue_button_can_override_a_grid_undercount(self) -> None:
        driver = FakeDriver(
            [7, 0, 0, 0, 0],
            readiness=[True, False, False, False, False],
        )

        result = SynthesisEngine(driver).run_round()

        self.assertEqual(result.equipment_combinations, 1)
        self.assertEqual(driver.actions.count("combine"), 1)

    def test_safety_limit_stops_a_possible_infinite_loop(self) -> None:
        driver = FakeDriver([9, 9])
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


if __name__ == "__main__":
    unittest.main()
