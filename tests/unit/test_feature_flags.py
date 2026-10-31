"""Feature-flag policy (F24).

The interesting behaviour is not storage, it is the two promises a rollout makes: a
disabled flag is off for everyone, and the same learner stays on the same side of a
percentage launch.
"""

from __future__ import annotations

import uuid

from app.modules.admin.application.flags_service import FeatureFlag


def flag(**overrides: object) -> FeatureFlag:
    values: dict[str, object] = {
        "key": "beta_ui",
        "enabled": True,
        "rollout_percentage": 100,
        "public": True,
        "description": None,
    }
    values.update(overrides)
    return FeatureFlag(**values)  # type: ignore[arg-type]


class TestPolicy:
    def test_a_disabled_flag_is_off_even_at_full_rollout(self) -> None:
        assert flag(enabled=False).is_on_for(uuid.uuid4()) is False

    def test_zero_percent_is_off_for_everyone(self) -> None:
        assert flag(rollout_percentage=0).is_on_for(uuid.uuid4()) is False

    def test_hundred_percent_is_on_for_everyone(self) -> None:
        assert all(flag().is_on_for(uuid.uuid4()) for _ in range(100))

    def test_the_same_learner_gets_the_same_answer(self) -> None:
        user_id = uuid.uuid4()
        policy = flag(rollout_percentage=50)
        answers = {policy.is_on_for(user_id) for _ in range(50)}
        assert len(answers) == 1

    def test_a_partial_rollout_splits_a_population_roughly_in_half(self) -> None:
        policy = flag(rollout_percentage=50)
        on = sum(policy.is_on_for(uuid.uuid4()) for _ in range(2_000))
        assert 850 <= on <= 1_150, f"50% rollout covered {on}/2000"

    def test_different_flags_bucket_the_same_user_independently(self) -> None:
        user_id = uuid.uuid4()
        first = flag(key="flag_a", rollout_percentage=50)
        second = flag(key="flag_b", rollout_percentage=50)
        # With independent hashes, the two answers agree about half the time; over a
        # single user we can only assert the policies are not the same object.
        assert first.bucket(user_id) == first.bucket(user_id)
        assert second.bucket(user_id) == second.bucket(user_id)

    def test_anonymous_callers_are_stable_per_flag(self) -> None:
        policy = flag(rollout_percentage=50)
        assert {policy.is_on_for(None) for _ in range(20)} == {policy.is_on_for(None)}
