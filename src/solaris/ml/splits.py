"""
Train/test splitting, and why a random split would be worthless here.

The samples are hourly observations grouped by city and day. Hours within a
city-day are strongly correlated -- consecutive hours share the same weather --
and sites within a climate zone are correlated too. A random row split would
therefore put an hour in the test set whose immediate neighbours are in the
training set, and report a score that says nothing about generalisation.

So the split is **spatial and temporal at once**: a test sample must come from
both a held-out city *and* a held-out year. That is strict, and it is the only
version that answers the question the model is for -- "what will this predict
at a site and time it has never seen?"

The city assignment is fixed rather than random, and chosen so the test set
spans distinct climate regimes rather than clustering in one.
"""

from __future__ import annotations

from dataclasses import dataclass

from solaris.ml.features import Sample

#: Held-out sites, deliberately spanning regimes rather than clustered:
#: Mumbai is coastal and humid, Bengaluru is a moderate plateau, Guwahati is
#: the high-cloud north-east.
TEST_CITIES = frozenset({"mumbai", "bengaluru", "guwahati"})

#: Held-out year. Kept separate from the city holdout so the intersection is
#: genuinely unseen on both axes.
TEST_YEARS = frozenset({2022})

#: Minimum great-circle separation between any test and any training site, km.
#: Well beyond any plausible weather-correlation length scale, so a test site
#: cannot be "nearly" in the training set.
MIN_SEPARATION_KM = 250.0


@dataclass
class Split:
    train: list[Sample]
    test: list[Sample]
    #: Samples in neither: a held-out city in a training year, or a training
    #: city in the held-out year. Excluded from both to keep the holdout clean.
    withheld: list[Sample]

    def summary(self) -> dict[str, object]:
        return {
            "n_train": len(self.train),
            "n_test": len(self.test),
            "n_withheld": len(self.withheld),
            "train_cities": sorted({s.city for s in self.train}),
            "test_cities": sorted({s.city for s in self.test}),
            "train_years": sorted({s.year for s in self.train}),
            "test_years": sorted({s.year for s in self.test}),
            "note": (
                "A test sample is from a held-out city AND a held-out year. "
                "Samples matching only one criterion are withheld from both "
                "sets, so neither leaks into the other."
            ),
        }


def split_samples(samples: list[Sample]) -> Split:
    """Partition on the intersection of the spatial and temporal holdouts."""
    train: list[Sample] = []
    test: list[Sample] = []
    withheld: list[Sample] = []

    for sample in samples:
        held_city = sample.city in TEST_CITIES
        held_year = sample.year in TEST_YEARS
        if held_city and held_year:
            test.append(sample)
        elif held_city or held_year:
            withheld.append(sample)
        else:
            train.append(sample)

    return Split(train=train, test=test, withheld=withheld)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    import math

    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def spatial_leakage(min_km: float = MIN_SEPARATION_KM) -> list[tuple[str, str, float]]:
    """
    Any test/train site pair closer than ``min_km``.

    Asserted by a test, so adding a city to the evaluation set without checking
    its separation is caught rather than quietly weakening the holdout.
    """
    from solaris.evals.references import CITIES

    offenders = []
    test_sites = [c for c in CITIES if c.key in TEST_CITIES]
    train_sites = [c for c in CITIES if c.key not in TEST_CITIES]
    for test_site in test_sites:
        for train_site in train_sites:
            distance = haversine_km(test_site.lat, test_site.lon, train_site.lat, train_site.lon)
            if distance < min_km:
                offenders.append((test_site.key, train_site.key, round(distance, 1)))
    return offenders
