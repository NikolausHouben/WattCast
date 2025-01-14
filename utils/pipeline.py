# pipeline.py

"""Contains all the functions to load and preprocess the data, as well as the config class."""
import sys
import darts
import numpy as np
import pandas as pd
import os
from darts import TimeSeries
from darts.dataprocessing.transformers import BoxCox, Scaler
import darts
from darts.utils.missing_values import extract_subseries
from darts.dataprocessing.transformers.boxcox import BoxCox
from darts.dataprocessing.transformers.scaler import Scaler
from darts.dataprocessing import Pipeline

from sklearn.preprocessing import MinMaxScaler, RobustScaler

import wandb

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .paths import CLEAN_DATA_DIR
from .data_utils import get_hdf_keys, review_subseries, select_first_week_of_each_month


class Config:
    """
    Class to store config parameters, to circumvent the wandb.config when combining multiple models when debugging
    """

    def __init__(self):
        self.data = {}

    def __getattr__(self, key):
        if key in self.data:
            return self.data[key]
        else:
            raise AttributeError(f"'Config' object has no attribute '{key}'")

    def __setattr__(self, key, value):
        if key == "data":
            # Allow normal assignment for the 'data' attribute
            super().__setattr__(key, value)
        else:
            self.data[key] = value

    def __delattr__(self, key):
        if key in self.data:
            del self.data[key]
        else:
            raise AttributeError(f"'Config' object has no attribute '{key}'")

    def __len__(self):
        return len(self.data)

    def keys(self):
        return self.data.keys()

    def values(self):
        return self.data.values()

    def items(self):
        return self.data.items()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    @classmethod
    def from_dict(cls, data, is_initial_config=True):
        config = cls()
        for key, value in data.items():
            config[key] = value  # Preserve nested dictionaries without converting

        if is_initial_config:
            config.derive_config_params()

        return config

    def copy(self):
        new_instance = Config()
        new_instance.data = self.data.copy()
        return new_instance

    def derive_config_params(self):
        if self.temp_resolution == 60:
            timestep_encoding = ["hour"]
        elif self.temp_resolution == 15:
            timestep_encoding = ["quarter"]
        else:
            timestep_encoding = ["hour", "minute"]

        datetime_encoders = {
            "cyclic": {"future": timestep_encoding},
            "datetime_attribute": {"future": self.datetime_attributes},
        }

        datetime_encoders = datetime_encoders if self.datetime_encodings else None
        self["datetime_encoders"] = datetime_encoders
        self.timesteps_per_hour = int(60 / self.temp_resolution)
        # input and output length for models
        self.n_lags = self.lookback_in_hours * self.timesteps_per_hour
        self.n_ahead = self.horizon_in_hours * self.timesteps_per_hour
        # evaluation stride, how often to evaluate the model, in this case we evaluate every n_ahead steps
        self.eval_stride = int(np.sqrt(self.n_ahead))

        return self


def data_pipeline(config, data):
    trg, cov = data["trg"], data["cov"]

    df_train, df_val, df_test = trg
    df_cov_train, df_cov_val, df_cov_test = cov

    # Heat wave covariate, categorical variable
    if config.heat_wave_binary:
        df_cov_train["heat_wave"] = df_cov_train[
            df_cov_train.columns[0]
        ] > df_cov_train[df_cov_train.columns[0]].quantile(0.95)
        df_cov_val["heat_wave"] = df_cov_val[df_cov_val.columns[0]] > df_cov_val[
            df_cov_val.columns[0]
        ].quantile(0.95)
        df_cov_test["heat_wave"] = df_cov_test[df_cov_test.columns[0]] > df_cov_test[
            df_cov_test.columns[0]
        ].quantile(0.95)

    # into darts format
    ts_train = darts.TimeSeries.from_dataframe(
        df_train, freq=str(config.temp_resolution) + "min"  # type: ignore
    )
    ts_train = extract_subseries(ts_train)
    ts_val = darts.TimeSeries.from_dataframe(
        df_val, freq=str(config.temp_resolution) + "min"  # type: ignore
    )
    ts_val = extract_subseries(ts_val)
    ts_test = darts.TimeSeries.from_dataframe(
        df_test, freq=str(config.temp_resolution) + "min"  # type: ignore
    )
    ts_test = extract_subseries(ts_test)

    # Covariates
    if config.weather_available:
        ts_cov_train = darts.TimeSeries.from_dataframe(
            df_cov_train, freq=str(config.temp_resolution) + "min"  # type: ignore
        )
        ts_cov_val = darts.TimeSeries.from_dataframe(
            df_cov_val, freq=str(config.temp_resolution) + "min"  # type: ignore
        )
        ts_cov_test = darts.TimeSeries.from_dataframe(
            df_cov_test, freq=str(config.temp_resolution) + "min"  # type: ignore
        )
    else:
        ts_cov_train = None
        ts_cov_val = None
        ts_cov_test = None

    # Reviewing subseries to make sure they are long enough
    ts_train, ts_cov_train = review_subseries(
        ts_train, config.n_lags + config.n_ahead, ts_cov_train
    )
    ts_val, ts_cov_val = review_subseries(
        ts_val, config.n_lags + config.n_ahead, ts_cov_val
    )
    ts_test, ts_cov_test = review_subseries(
        ts_test, config.n_lags + config.n_ahead, ts_cov_test
    )

    # Preprocessing Pipeline, global fit is important to turn on because we have split the entire ts into multiple timeseries at nans
    # but want to use the same params for all of them
    # Missing values have been filled in the 'data_prep.ipynb', so we don't need to do that here
    pipeline = Pipeline(
        [
            BoxCox(global_fit=True)
            if config.boxcox
            else Scaler(
                MinMaxScaler(), global_fit=True
            ),  # double scale in case boxcox is turned off
            Scaler(MinMaxScaler(), global_fit=True),
        ]
    )
    ts_train_piped = pipeline.fit_transform(ts_train)
    ts_val_piped = pipeline.transform(ts_val)
    ts_test_piped = pipeline.transform(ts_test)

    # Weather Pipeline
    if config.weather_available:
        pipeline_weather = Pipeline([Scaler(RobustScaler(), global_fit=True)])
        ts_train_weather_piped = pipeline_weather.fit_transform(ts_cov_train)
        ts_val_weather_piped = pipeline_weather.transform(ts_cov_val)
        ts_test_weather_piped = pipeline_weather.transform(ts_cov_test)
    else:
        ts_train_weather_piped = None
        ts_val_weather_piped = None
        ts_test_weather_piped = None

    piped_data = (
        ts_train_piped,
        ts_val_piped,
        ts_test_piped,
        ts_train_weather_piped,
        ts_val_weather_piped,
        ts_test_weather_piped,
    )

    return piped_data, pipeline


def load_auxiliary_training_data(config):
    """To enhance the current locations trainign data with the val and test sets of locations on the same scale"""

    list_auxiliary_data = []
    if config.use_auxiliary_data:
        for auxiliary_location in get_hdf_keys(CLEAN_DATA_DIR)[0][
            config.spatial_scale + ".h5"
        ]:
            auxiliary_config = config.copy()
            if auxiliary_location != config.location:
                auxiliary_config.location = auxiliary_location
                auxiliary_data = load_data(auxiliary_config)
                list_auxiliary_data.append(auxiliary_data)

    return list_auxiliary_data[:4]


def pipeline_auxiliary_data(config, list_auxiliary_data):
    if len(list_auxiliary_data) == 0:
        return [], []

    auxiliary_training_data_trg = []
    auxiliary_training_data_cov = []
    for auxiliary_data in list_auxiliary_data:
        auxiliary_piped_data, _ = data_pipeline(config, auxiliary_data)
        (
            _,
            aux_ts_val_piped,
            aux_ts_test_piped,
            _,
            aux_ts_val_weather_piped,
            aux_ts_test_weather_piped,
        ) = auxiliary_piped_data
        auxiliary_training_data_trg.append(aux_ts_val_piped[0])
        auxiliary_training_data_cov.append(aux_ts_val_weather_piped[0])  # type: ignore
        auxiliary_training_data_trg.append(aux_ts_test_piped[0])
        auxiliary_training_data_cov.append(aux_ts_test_weather_piped[0])  # type: ignore

    return auxiliary_training_data_trg, auxiliary_training_data_cov


def get_best_run_config(project_name, metric, model, scale, location: str):
    """

    Returns the config of the best run of a sweep for a given model and location.

    """

    api = wandb.Api()
    sweeps = []
    for project in api.projects():
        if project_name == project.name:
            sweeps = project.sweeps()

    config = None
    name = None
    for sweep in sweeps:
        if model in sweep.name and scale in sweep.name and location in sweep.name:
            best_run = sweep.best_run(order=metric)
            config = best_run.config
            config = Config().from_dict(config)
            name = best_run.name
            model_abbr = config.model_abbr
            print(f"Fetched sweep with name {name} for model {model_abbr}")

    if config == None:
        print(
            f"Could not find a sweep for model {model} and scale {scale} in project {project_name}."
        )
        config = Config()
        config.model_abbr = model

    return config, name


def load_data(config):
    """Loads the data from disk and returns it in a dictionary, along with the config"""

    # Loading Data
    df_train = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/train_target",
    )

    df_val = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/val_target",
    )
    df_test = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/test_target",
    )

    df_cov_train = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/train_cov",
    )
    df_cov_val = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/val_cov",
    )
    df_cov_test = pd.read_hdf(
        os.path.join(CLEAN_DATA_DIR, f"{config.spatial_scale}.h5"),
        key=f"{config.location}/{config.temp_resolution}min/test_cov",
    )

    # making the dataframe smaller for tuning to save time
    if config.tuning:
        df_train = select_first_week_of_each_month(df_train, config)
        df_cov_train = df_cov_train.reindex(df_train.index)

    data = {
        "trg": (df_train, df_val, df_test),
        "cov": (df_cov_train, df_cov_val, df_cov_test),
    }

    return data
