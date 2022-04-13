#
#   Copyright 2022 Logical Clocks AB
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import warnings
from hsfs import engine, training_dataset_feature
from hsfs.client import exceptions
from hsfs.core import (
    tags_api,
    storage_connector_api,
    transformation_function_engine,
    feature_view_api,
    code_engine,
    statistics_engine,
    training_dataset_engine,
    query_constructor_api,
    job
)


class FeatureViewEngine:
    ENTITY_TYPE = "featureview"
    _TRAINING_DATA_API_PATH = "trainingdatasets"
    _OVERWRITE = "overwrite"
    _APPEND = "append"

    def __init__(self, feature_store_id):
        self._feature_store_id = feature_store_id

        self._feature_view_api = feature_view_api.FeatureViewApi(
            feature_store_id
        )
        self._tags_api = tags_api.TagsApi(feature_store_id, self.ENTITY_TYPE)
        self._storage_connector_api = storage_connector_api.StorageConnectorApi(
            feature_store_id
        )
        self._transformation_function_engine = (
            transformation_function_engine.TransformationFunctionEngine(
                feature_store_id
            )
        )
        self._td_code_engine = code_engine.CodeEngine(feature_store_id,
                                                   self._TRAINING_DATA_API_PATH)
        self._statistics_engine = statistics_engine.StatisticsEngine(
            feature_store_id, self._TRAINING_DATA_API_PATH
        )
        self._training_dataset_engine = training_dataset_engine.TrainingDatasetEngine(
            feature_store_id
        )
        self._query_constructor_api = query_constructor_api.QueryConstructorApi()

    def save(self, feature_view_obj):
        if feature_view_obj.label:
            feature_view_obj._features.append([
                training_dataset_feature.TrainingDatasetFeature(
                    name=label_name, label=True
                )
                for label_name in feature_view_obj.label
            ])
        self._transformation_function_engine.attach_transformation_fn(
            feature_view_obj
        )
        return self._feature_view_api.post(feature_view_obj)

    def get(self, name, version=None):
        if version:
            return self._feature_view_api.get_by_name_version(name, version)
        else:
            return self._feature_view_api.get_by_name(name)

    def delete(self, name, version=None):
        if version:
            return self._feature_view_api.delete_by_name_version(name, version)
        else:
            return self._feature_view_api.delete_by_name(name)

    def get_batch_query(self, feature_view_obj, start_time,
                               end_time):
        return self._feature_view_api.get_batch_query(
            feature_view_obj.name, feature_view_obj.version, start_time,
            end_time, is_python_engine=engine.get_type() == "python"
        )

    def get_batch_query_string(self, feature_view_obj, start_time, end_time):
        query_obj = self._feature_view_api.get_batch_query(
            feature_view_obj.name, feature_view_obj.version, start_time,
            end_time, is_python_engine=engine.get_type() == "python"
        )
        fs_query = self._query_constructor_api.construct_query(query_obj)
        if fs_query.pit_query is not None:
            return fs_query.pit_query
        return fs_query.query

    def get_attached_transformation_fn(self, name, version):
        transformation_functions = self._feature_view_api.get_attached_transformation_fn(
            name, version
        )
        if isinstance(transformation_functions, list):
            transformation_functions_dict = dict([
                (tf.name, tf.transformation_function) for
                tf in transformation_functions
            ])
        else:
            transformation_functions_dict = {
                transformation_functions.name:
                    transformation_functions.transformation_function
            }
        return transformation_functions_dict

    def create_training_dataset(self, feature_view_obj,
                                training_dataset_obj, user_write_options):
        if (len(training_dataset_obj.splits) > 0 and
            training_dataset_obj.train_split is None):
            training_dataset_obj.train_split = "train"
            warnings.warn(
                "Training dataset splits were defined but no `train_split` (the name of the split that is going to be "
                "used for training) was provided. Setting this property to `train`. The statistics of this "
                "split will be used for transformation functions."
            )

        updated_instance = self._create_training_data_metadata(
            feature_view_obj, training_dataset_obj
        )
        td_job = self.compute_training_dataset(
            feature_view_obj,
            user_write_options,
            training_dataset_obj=training_dataset_obj
        )
        return updated_instance, td_job

    def get_training_data(self, feature_view_obj,
                                training_dataset_obj, read_options, split=None):
        td_updated = None
        should_compute_statistics = True
        # check if provided td version has already existed.
        if training_dataset_obj.version:
            try:
                td_updated = self._get_training_data_metadata(
                    feature_view_obj, training_dataset_obj.version)
                should_compute_statistics = False
            except exceptions.RestAPIError as e:
                # error code: 270012, error msg: Training dataset wasn't found.
                if e.response.json().get("errorCode", "") != 270012:
                    raise e

        if td_updated is None:
            td_updated = self._create_training_data_metadata(
            feature_view_obj, training_dataset_obj
        )

        try:
            df = self._read_from_storage_connector(
                training_dataset_obj, split, read_options
            )
        except:
            # todo feature view: refine exception
            query = self.get_batch_query(
                feature_view_obj,
                start_time=training_dataset_obj.start_time,
                end_time=training_dataset_obj.end_time
            )
            df = engine.get_instance().get_training_data(
                training_dataset_obj, feature_view_obj,
                query, read_options
            )

        if should_compute_statistics:
            self.compute_training_dataset_statistics(
                feature_view_obj, training_dataset_obj, df
            )

        # read from hopsfs after job finished
        if isinstance(df, job.Job):
            df = self._read_from_storage_connector(
                training_dataset_obj, split, read_options
            )

        return td_updated, df

    def _read_from_storage_connector(self, training_data_obj, split, read_options):
        if split is not None:
            path = training_data_obj.location + "/" + str(split)
        else:
            path = training_data_obj.location + "/" + training_data_obj.name
        return training_data_obj.storage_connector.read(
            # always read from materialized dataset, not query object
            query=None,
            data_format=training_data_obj.data_format,
            options=read_options,
            path=path,
        )

    # This method is used by hsfs_utils to launch a job for python client
    def compute_training_dataset(self, feature_view_obj, user_write_options,
                                 training_dataset_obj=None,
                                 training_dataset_version=None):
        if training_dataset_obj:
            pass
        elif training_dataset_version:
            training_dataset_obj = self._get_training_data_metadata(
                feature_view_obj, training_dataset_version
            )
        else:
            raise ValueError("No training dataset object or version is provided")

        batch_query = self.get_batch_query(
            feature_view_obj, training_dataset_obj.start_time, training_dataset_obj.end_time
        )
        td_job = engine.get_instance().write_training_dataset(
            training_dataset_obj, batch_query, user_write_options,
            self._OVERWRITE, feature_view_obj=feature_view_obj
        )
        self._td_code_engine.save_code(training_dataset_obj)
        if engine.get_type() == "spark":
            if training_dataset_obj.splits:
                td_df = dict([(split, self._training_dataset_engine.read(
                    training_dataset_obj, split, {})) for
                    split in training_dataset_obj.splits
                ])
            else:
                td_df = self._training_dataset_engine.read(
                    training_dataset_obj, None, {})
        else:
            td_df = None
        # currently we do not save the training dataset statistics config for training datasets
        self.compute_training_dataset_statistics(
            feature_view_obj, training_dataset_obj, td_df)
        return td_job

    def compute_training_dataset_statistics(self, feature_view_obj,
                                            training_dataset_obj,
                                            td_df):
        # In Python engine, a job computes a statistics along with training data
        if training_dataset_obj.statistics_config.enabled and engine.get_type() == "spark":
            if training_dataset_obj.splits:
                if not isinstance(td_df, dict):
                    raise ValueError(
                        "Provided dataframes should be in dict format "
                        "'split': dataframe")
                return self._statistics_engine.register_split_statistics(
                    training_dataset_obj, feature_dataframes=td_df,
                    feature_view_obj=feature_view_obj)
            else:
                return self._statistics_engine.compute_statistics(
                    training_dataset_obj, feature_dataframe=td_df,
                    feature_view_obj=feature_view_obj)

    def _get_training_data_metadata(self, feature_view_obj, training_dataset_version):
        td = self._feature_view_api.get_training_dataset_by_version(
                    feature_view_obj.name, feature_view_obj.version,
                    training_dataset_version)
        # schema and transformation functions need to be set for writing training data or feature serving
        td.schema = feature_view_obj.schema
        td.transformation_functions = self.get_attached_transformation_fn(
            feature_view_obj.name, feature_view_obj.version
        )
        return td

    def _create_training_data_metadata(self, feature_view_obj,
                                       training_dataset_obj):
        td = self._feature_view_api.create_training_dataset(
            feature_view_obj.name, feature_view_obj.version,
            training_dataset_obj)
        td.schema = feature_view_obj.schema
        td.transformation_functions = (
            feature_view_obj.transformation_functions
        )
        return td