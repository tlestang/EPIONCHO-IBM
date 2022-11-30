from dataclasses import dataclass

import numpy as np

import epioncho_ibm.blackfly as blackfly
import epioncho_ibm.utils as utils
from epioncho_ibm.types import Array

from .params import BlackflyParams, TreatmentParams, WormParams

__all__ = [
    "WormGroup",
    "change_in_worms",
    "get_delayed_males_and_females",
    "calc_new_worms",
]


@dataclass
class WormGroup:
    male: Array.WormCat.Person.Int
    infertile: Array.WormCat.Person.Int
    fertile: Array.WormCat.Person.Int

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WormGroup):
            return (
                np.array_equal(self.male, other.male)
                and np.array_equal(self.infertile, other.infertile)
                and np.array_equal(self.fertile, other.fertile)
            )
        else:
            return False

    @classmethod
    def from_population(cls, population: int):
        return cls(
            male=np.zeros(population, dtype=int),
            infertile=np.zeros(population, dtype=int),
            fertile=np.zeros(population, dtype=int),
        )


def _calc_dead_and_aging_worms_single_group(
    current_worms: Array.WormCat.Person.Int,
    mortalities: Array.WormCat.Float,
    worm_age_rate: float,
) -> tuple[Array.WormCat.Person.Int, Array.WormCat.Person.Int]:
    assert current_worms.ndim == 2
    n_people = current_worms.shape[1]

    if mortalities.ndim == 1:
        mortalities = np.tile(mortalities, (n_people, 1)).T

    dead_worms: Array.WormCat.Person.Int = np.random.binomial(
        n=current_worms,
        p=mortalities,
        size=current_worms.shape,
    )
    aging_worms: Array.WormCat.Person.Int = np.random.binomial(
        n=current_worms - dead_worms,
        p=worm_age_rate,
        size=current_worms.shape,
    )
    return dead_worms, aging_worms


def _calc_dead_and_aging_worms(
    current_worms: WormGroup,
    female_mortalities: Array.WormCat.Float | Array.WormCat.Person.Float,
    male_mortalities: Array.WormCat.Float,
    worm_age_rate: float,
) -> tuple[WormGroup, WormGroup]:
    dead_male, aging_male = _calc_dead_and_aging_worms_single_group(
        current_worms=current_worms.male,
        mortalities=male_mortalities,
        worm_age_rate=worm_age_rate,
    )

    dead_infertile, aging_infertile = _calc_dead_and_aging_worms_single_group(
        current_worms=current_worms.infertile,
        mortalities=female_mortalities,
        worm_age_rate=worm_age_rate,
    )

    dead_fertile, aging_fertile = _calc_dead_and_aging_worms_single_group(
        current_worms=current_worms.fertile,
        mortalities=female_mortalities,
        worm_age_rate=worm_age_rate,
    )

    dead = WormGroup(male=dead_male, infertile=dead_infertile, fertile=dead_fertile)
    aging = WormGroup(male=aging_male, infertile=aging_infertile, fertile=aging_fertile)
    return dead, aging


def _calc_new_worms_from_inside(
    current_worms: Array.WormCat.Person.Int,
    dead_worms: Array.WormCat.Person.Int,
    aging_worms: Array.WormCat.Person.Int,
    prob: float | Array.Person.Float,
) -> Array.WormCat.Person.Int:
    # trans.fc
    delta_female_worms = current_worms - dead_worms - aging_worms
    delta_female_worms[delta_female_worms < 0] = 0

    current_worms_shape = current_worms.shape
    probabilities = prob
    assert len(current_worms_shape) in (1, 2)
    if len(current_worms_shape) == 1:
        probabilities = np.repeat(prob, current_worms.shape[0]).reshape(
            current_worms_shape
        )

    if delta_female_worms.any():
        new_worms = np.random.binomial(
            n=delta_female_worms,
            p=probabilities,
            size=current_worms.shape,
        )
    else:
        new_worms = np.zeros_like(current_worms)
    return new_worms


def process_treatment(
    worm_params: WormParams,
    treatment_params: TreatmentParams | None,
    delta_time: float,
    n_people: int,
    coverage_in: Array.Person.Bool | None,
    initial_treatment_times: Array.Treatments.Float | None,
    time_of_last_treatment: Array.Person.Float | None,
    current_time: float,
    mortalities: Array.WormCat.Float,
) -> tuple[
    Array.WormCat.Float | Array.WormCat.Person.Float,
    Array.Person.Float,
    Array.Person.Float | None,
]:
    # approach assumes individuals which are moved from fertile to non
    # fertile class due to treatment re enter fertile class at standard rate

    female_mortalities: Array.WormCat.Float = mortalities  # mort.fems
    fertile_to_non_fertile_rate: Array.Person.Float = np.zeros(n_people)

    # We'll create a new array (a copy) only when needed, see below if-
    modified_time_of_last_treatment = time_of_last_treatment

    if treatment_params is not None and current_time > treatment_params.start_time:
        assert modified_time_of_last_treatment is not None
        assert initial_treatment_times is not None
        during_treatment = np.any(
            (current_time <= initial_treatment_times)
            & (initial_treatment_times < current_time + delta_time)
        )
        if during_treatment and current_time <= treatment_params.stop_time:
            female_mortalities = np.tile(mortalities, (n_people, 1))
            assert coverage_in is not None
            assert coverage_in.shape == (n_people,)
            modified_time_of_last_treatment = modified_time_of_last_treatment.copy()
            modified_time_of_last_treatment[coverage_in] = current_time  # treat.vec
            # params.permanent_infertility is the proportion of female worms made permanently infertile, killed for simplicity
            female_mortalities[coverage_in] += worm_params.permanent_infertility

        time_since_treatment = current_time - modified_time_of_last_treatment  # tao

        # individuals which have been treated get additional infertility rate
        lam_m_temp = np.where(
            modified_time_of_last_treatment == np.nan, 0, worm_params.lam_m
        )
        fertile_to_non_fertile_rate: Array.Person.Float = np.nan_to_num(
            delta_time * lam_m_temp * np.exp(-worm_params.phi * time_since_treatment)
        )

    return (
        female_mortalities.T,
        worm_params.lambda_zero * delta_time + fertile_to_non_fertile_rate,
        modified_time_of_last_treatment,
    )


def change_in_worms(
    current_worms: WormGroup,
    worm_params: WormParams,
    treatment_params: TreatmentParams | None,
    delta_time: float,
    n_people: int,
    delayed_females: Array.Person.Int,
    delayed_males: Array.Person.Int,
    mortalities: Array.WormCat.Float,
    coverage_in: Array.Person.Bool | None,
    initial_treatment_times: Array.Treatments.Float | None,
    current_time: float,
    time_of_last_treatment: Array.Person.Float | None,
) -> tuple[WormGroup, Array.Person.Float | None]:
    # TODO: time_of_last_treatment is modified inside, change this!
    female_mortalities, lambda_zero_in, time_of_last_treatment = process_treatment(
        worm_params=worm_params,
        treatment_params=treatment_params,
        delta_time=delta_time,
        n_people=n_people,
        coverage_in=coverage_in,
        initial_treatment_times=initial_treatment_times,
        time_of_last_treatment=time_of_last_treatment,
        current_time=current_time,
        mortalities=mortalities,
    )

    worm_age_rate = delta_time / worm_params.worms_aging

    dead, aging = _calc_dead_and_aging_worms(
        current_worms=current_worms,
        female_mortalities=female_mortalities,
        male_mortalities=mortalities,
        worm_age_rate=worm_age_rate,
    )

    lagged_aging_male = utils.lag_array(delayed_males, aging.male)
    lagged_aging_infertile = utils.lag_array(delayed_females, aging.infertile)
    lagged_aging_fertile = utils.lag_array(
        np.zeros(aging.fertile.shape[1], dtype="int"), aging.fertile
    )

    new_infertile_from_inside = _calc_new_worms_from_inside(
        current_worms=current_worms.fertile,
        dead_worms=dead.fertile,
        aging_worms=aging.fertile,
        prob=lambda_zero_in,
    )  # new.worms.nf.fi

    # individuals which still have non fertile worms in an age compartment after death and aging

    omega = worm_params.omega * delta_time  # becoming fertile
    new_fertile_from_inside = _calc_new_worms_from_inside(
        current_worms=current_worms.infertile,
        dead_worms=dead.infertile,
        aging_worms=aging.infertile,
        prob=omega,
    )  # new.worms.f.fi TODO: Are these the right way round?

    delta_fertile = new_fertile_from_inside - new_infertile_from_inside

    infertile_excl_transiting = current_worms.infertile - delta_fertile - dead.infertile
    fertile_excl_transiting = current_worms.fertile + delta_fertile - dead.fertile

    new_male = current_worms.male + lagged_aging_male - aging.male - dead.male
    new_infertile = infertile_excl_transiting - aging.infertile + lagged_aging_infertile
    new_fertile = fertile_excl_transiting - aging.fertile + lagged_aging_fertile

    assert np.all(
        (new_male >= 0) & (new_infertile >= 0) & (new_fertile >= 0)
    ), "Worms became negative!"
    return (
        WormGroup(male=new_male, infertile=new_infertile, fertile=new_fertile),
        time_of_last_treatment,
    )


def get_delayed_males_and_females(
    worm_delay: Array.WormDelay.Person.Int, worm_sex_ratio: float
) -> tuple[Array.Person.Int, Array.Person.Int]:
    final_column: Array.Person.Int = np.array(worm_delay[-1], dtype=int)
    last_males = np.random.binomial(n=final_column, p=worm_sex_ratio)  # new.worms.m
    last_females = final_column - last_males  # new.worms.nf
    return last_males, last_females


def calc_new_worms(
    L3: Array.Person.Float,
    blackfly_params: BlackflyParams,
    delta_time: float,
    total_exposure: Array.Person.Float,
    n_people: int,
) -> Array.Person.Int:
    new_rate = blackfly.w_plus_one_rate(
        blackfly_params,
        delta_time,
        float(np.mean(L3)),
        total_exposure,
    )
    assert not np.any(new_rate > 10**10)
    new_worms = np.random.poisson(lam=new_rate, size=n_people)
    return new_worms
