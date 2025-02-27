import logging
import os
import time
import importlib
import sys

from mlflow.pipelines.artifacts import DataframeArtifact
from mlflow.pipelines.cards import BaseCard
from mlflow.pipelines.step import BaseStep
from mlflow.pipelines.step import StepClass
from mlflow.pipelines.utils.execution import get_step_output_path
from mlflow.pipelines.utils.step import get_pandas_data_profiles
from mlflow.exceptions import MlflowException, INVALID_PARAMETER_VALUE, BAD_REQUEST


_logger = logging.getLogger(__name__)


_SPLIT_HASH_BUCKET_NUM = 1000
_INPUT_FILE_NAME = "dataset.parquet"
_OUTPUT_TRAIN_FILE_NAME = "train.parquet"
_OUTPUT_VALIDATION_FILE_NAME = "validation.parquet"
_OUTPUT_TEST_FILE_NAME = "test.parquet"
_MULTI_PROCESS_POOL_SIZE = 8


def _make_elem_hashable(elem):
    import numpy as np

    if isinstance(elem, list):
        return tuple(_make_elem_hashable(e) for e in elem)
    elif isinstance(elem, dict):
        return tuple((_make_elem_hashable(k), _make_elem_hashable(v)) for k, v in elem.items())
    elif isinstance(elem, np.ndarray):
        return elem.shape, tuple(elem.flatten(order="C"))
    else:
        return elem


def _get_split_df(input_df, hash_buckets, split_ratios):
    # split dataset into train / validation / test splits
    train_ratio, validation_ratio, test_ratio = split_ratios
    ratio_sum = train_ratio + validation_ratio + test_ratio
    train_bucket_end = train_ratio / ratio_sum
    validation_bucket_end = (train_ratio + validation_ratio) / ratio_sum
    train_df = input_df[hash_buckets.map(lambda x: x < train_bucket_end)]
    validation_df = input_df[
        hash_buckets.map(lambda x: train_bucket_end <= x < validation_bucket_end)
    ]
    test_df = input_df[hash_buckets.map(lambda x: x >= validation_bucket_end)]

    empty_splits = [
        split_name
        for split_name, split_df in [
            ("train split", train_df),
            ("validation split", validation_df),
            ("test split", test_df),
        ]
        if len(split_df) == 0
    ]
    if len(empty_splits) > 0:
        _logger.warning(f"The following input dataset splits are empty: {','.join(empty_splits)}.")
    return train_df, validation_df, test_df


def _parallelize(data, func):
    import numpy as np
    import pandas as pd
    from multiprocessing import Pool

    data_split = np.array_split(data, _MULTI_PROCESS_POOL_SIZE)
    pool = Pool(_MULTI_PROCESS_POOL_SIZE)
    data = pd.concat(pool.map(func, data_split))
    pool.close()
    pool.join()
    return data


def _run_on_subset(func, data_subset):
    return data_subset.applymap(func)


def _parallelize_on_rows(data, func):
    from functools import partial

    return _parallelize(data, partial(_run_on_subset, func))


def _hash_pandas_dataframe(input_df):
    from pandas.util import hash_pandas_object

    hashed_input_df = _parallelize_on_rows(input_df, _make_elem_hashable)
    return hash_pandas_object(hashed_input_df)


def _create_hash_buckets(input_df):
    # Create hash bucket used for splitting dataset
    # Note: use `hash_pandas_object` instead of python builtin hash because it is stable
    # across different process runs / different python versions
    start_time = time.time()
    hash_buckets = _hash_pandas_dataframe(input_df).map(
        lambda x: (x % _SPLIT_HASH_BUCKET_NUM) / _SPLIT_HASH_BUCKET_NUM
    )
    execution_duration = time.time() - start_time
    _logger.debug(
        f"Creating hash buckets on input dataset containing {len(input_df)} "
        f"rows consumes {execution_duration} seconds."
    )
    return hash_buckets


def _validate_user_code_output(post_split, train_df, validation_df, test_df):
    try:
        (
            post_filter_train_df,
            post_filter_validation_df,
            post_filter_test_df,
        ) = post_split(train_df, validation_df, test_df)
    except Exception:
        raise MlflowException(
            message="Error in cleaning up the data frame post split step."
            " Expected output is a tuple with (train_df, validation_df, test_df)"
        ) from None

    import pandas as pd

    for (post_split_df, pre_split_df, split_type) in [
        [post_filter_train_df, train_df, "train"],
        [post_filter_validation_df, validation_df, "validation"],
        [post_filter_test_df, test_df, "test"],
    ]:
        if not isinstance(post_split_df, pd.DataFrame):
            raise MlflowException(
                message="The split data is not a DataFrame, please return the correct data."
            ) from None
        if list(pre_split_df.columns) != list(post_split_df.columns):
            raise MlflowException(
                message="The number of columns post split step are different."
                f" Column list for {split_type} dataset pre-slit is {list(pre_split_df.columns)}"
                f" and post split is {list(post_split_df.columns)}. "
                "Split filter function should be used to filter rows rather than filtering columns."
            ) from None

    return (
        post_filter_train_df,
        post_filter_validation_df,
        post_filter_test_df,
    )


class SplitStep(BaseStep):
    def _validate_and_apply_step_config(self):
        self.run_end_time = None
        self.execution_duration = None
        self.num_dropped_rows = None

        self.target_col = self.step_config.get("target_col")
        self.skip_data_profiling = self.step_config.get("skip_data_profiling", False)
        if self.target_col is None:
            raise MlflowException(
                "Missing target_col config in pipeline config.",
                error_code=INVALID_PARAMETER_VALUE,
            )
        self.skip_data_profiling = self.step_config.get("skip_data_profiling", False)

        self.split_ratios = self.step_config.get("split_ratios", [0.75, 0.125, 0.125])
        if not (
            isinstance(self.split_ratios, list)
            and len(self.split_ratios) == 3
            and all(isinstance(x, (int, float)) and x > 0 for x in self.split_ratios)
        ):
            raise MlflowException(
                "Config split_ratios must be a list containing 3 positive numbers."
            )

    def _build_profiles_and_card(self, train_df, validation_df, test_df) -> BaseCard:
        def _set_target_col_as_first(df, target_col):
            columns = list(df.columns)
            col = columns.pop(columns.index(target_col))
            return df[[col] + columns]

        # Build card
        card = BaseCard(self.pipeline_name, self.name)

        if not self.skip_data_profiling:
            # Build profiles for input dataset, and train / validation / test splits
            train_df = _set_target_col_as_first(train_df, self.target_col)
            validation_df = _set_target_col_as_first(validation_df, self.target_col)
            test_df = _set_target_col_as_first(test_df, self.target_col)
            data_profile = get_pandas_data_profiles(
                [
                    ["Train", train_df.reset_index(drop=True)],
                    ["Validation", validation_df.reset_index(drop=True)],
                    ["Test", test_df.reset_index(drop=True)],
                ]
            )

            # Tab #1 - #3: data profiles for train/validation and test.
            card.add_tab("Compare Splits", "{{PROFILE}}").add_pandas_profile(
                "PROFILE", data_profile
            )

        # Tab #4: run summary.
        (
            card.add_tab(
                "Run Summary",
                """
                {{ SCHEMA_LOCATION }}
                {{ TRAIN_SPLIT_NUM_ROWS }}
                {{ VALIDATION_SPLIT_NUM_ROWS }}
                {{ TEST_SPLIT_NUM_ROWS }}
                {{ NUM_DROPPED_ROWS }}
                {{ EXE_DURATION}}
                {{ LAST_UPDATE_TIME }}
                """,
            )
            .add_markdown(
                "NUM_DROPPED_ROWS", f"**Number of dropped rows:** `{self.num_dropped_rows}`"
            )
            .add_markdown(
                "TRAIN_SPLIT_NUM_ROWS", f"**Number of train dataset rows:** `{len(train_df)}`"
            )
            .add_markdown(
                "VALIDATION_SPLIT_NUM_ROWS",
                f"**Number of validation dataset rows:** `{len(validation_df)}`",
            )
            .add_markdown(
                "TEST_SPLIT_NUM_ROWS", f"**Number of test dataset rows:** `{len(test_df)}`"
            )
        )

        return card

    def _run(self, output_directory):
        import pandas as pd

        run_start_time = time.time()

        # read ingested dataset
        ingested_data_path = get_step_output_path(
            pipeline_root_path=self.pipeline_root,
            step_name="ingest",
            relative_path=_INPUT_FILE_NAME,
        )
        input_df = pd.read_parquet(ingested_data_path)

        # drop rows which target value is missing
        raw_input_num_rows = len(input_df)
        # Make sure the target column is actually present in the input DF.
        if self.target_col not in input_df.columns:
            raise MlflowException(
                f"Target column '{self.target_col}' not found in ingested dataset.",
                error_code=INVALID_PARAMETER_VALUE,
            )
        input_df = input_df.dropna(how="any", subset=[self.target_col])
        self.num_dropped_rows = raw_input_num_rows - len(input_df)

        # split dataset
        hash_buckets = _create_hash_buckets(input_df)
        train_df, validation_df, test_df = _get_split_df(input_df, hash_buckets, self.split_ratios)
        # Import from user function module to process dataframes
        post_split_config = self.step_config.get("post_split_method", None)
        post_split_filter_config = self.step_config.get("post_split_filter_method", None)
        if post_split_config is not None:
            (post_split_module_name, post_split_fn_name) = post_split_config.rsplit(".", 1)
            sys.path.append(self.pipeline_root)
            post_split = getattr(
                importlib.import_module(post_split_module_name), post_split_fn_name
            )
            _logger.debug(f"Running {post_split_fn_name} on train, validation and test datasets.")
            (
                train_df,
                validation_df,
                test_df,
            ) = _validate_user_code_output(post_split, train_df, validation_df, test_df)

        elif post_split_filter_config is not None:
            (
                post_split_filter_module_name,
                post_split_filter_fn_name,
            ) = post_split_filter_config.rsplit(".", 1)
            sys.path.append(self.pipeline_root)
            post_split_filter = getattr(
                importlib.import_module(post_split_filter_module_name), post_split_filter_fn_name
            )
            _logger.debug(
                f"Running {post_split_filter_fn_name} on train, validation and test datasets."
            )
            train_df = train_df[post_split_filter(train_df)]
            validation_df = validation_df[post_split_filter(validation_df)]
            test_df = test_df[post_split_filter(test_df)]

        if min(len(train_df), len(validation_df), len(test_df)) < 4:
            raise MlflowException(
                f"Train, validation, and testing datasets cannot be less than 4 rows. Train has "
                f"{len(train_df)} rows, validation has {len(validation_df)} rows, and test has "
                f"{len(test_df)} rows.",
                error_code=BAD_REQUEST,
            )
        # Output train / validation / test splits
        train_df.to_parquet(os.path.join(output_directory, _OUTPUT_TRAIN_FILE_NAME))
        validation_df.to_parquet(os.path.join(output_directory, _OUTPUT_VALIDATION_FILE_NAME))
        test_df.to_parquet(os.path.join(output_directory, _OUTPUT_TEST_FILE_NAME))

        self.run_end_time = time.time()
        self.execution_duration = self.run_end_time - run_start_time
        return self._build_profiles_and_card(train_df, validation_df, test_df)

    @classmethod
    def from_pipeline_config(cls, pipeline_config, pipeline_root):
        step_config = {}
        if pipeline_config.get("steps", {}).get("split", {}) is not None:
            step_config.update(pipeline_config.get("steps", {}).get("split", {}))
        step_config["target_col"] = pipeline_config.get("target_col")
        return cls(step_config, pipeline_root)

    @property
    def name(self):
        return "split"

    def get_artifacts(self):
        return [
            DataframeArtifact(
                "training_data", self.pipeline_root, self.name, _OUTPUT_TRAIN_FILE_NAME
            ),
            DataframeArtifact(
                "validation_data", self.pipeline_root, self.name, _OUTPUT_VALIDATION_FILE_NAME
            ),
            DataframeArtifact("test_data", self.pipeline_root, self.name, _OUTPUT_TEST_FILE_NAME),
        ]

    def step_class(self):
        return StepClass.TRAINING
