#
#   Copyright 2020 Logical Clocks AB
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

import os
import json
import importlib.util

import numpy as np
import pandas as pd
import avro

# in case importing in %%local
try:
    from pyspark import SparkFiles
    from pyspark.sql import SparkSession, DataFrame
    from pyspark.rdd import RDD
    from pyspark.sql.functions import struct, concat, col, lit, from_json
    from pyspark.sql.avro.functions import from_avro, to_avro
except ImportError:
    pass

from hsfs import feature, training_dataset_feature, client, util
from hsfs.storage_connector import StorageConnector
from hsfs.client.exceptions import FeatureStoreException
from hsfs.core import hudi_engine
from hsfs.constructor import query


class Engine:
    HIVE_FORMAT = "hive"
    KAFKA_FORMAT = "kafka"

    APPEND = "append"
    OVERWRITE = "overwrite"

    def __init__(self):
        self._spark_session = SparkSession.builder.enableHiveSupport().getOrCreate()
        self._spark_context = self._spark_session.sparkContext
        self._jvm = self._spark_context._jvm

        self._spark_session.conf.set("hive.exec.dynamic.partition", "true")
        self._spark_session.conf.set("hive.exec.dynamic.partition.mode", "nonstrict")
        self._spark_session.conf.set("spark.sql.hive.convertMetastoreParquet", "false")

        if importlib.util.find_spec("pydoop"):
            # If we are on Databricks don't setup Pydoop as it's not available and cannot be easily installed.
            util.setup_pydoop()

    def sql(self, sql_query, feature_store, connector, dataframe_type, read_options):
        if not connector:
            result_df = self._sql_offline(sql_query, feature_store)
        else:
            result_df = connector.read(sql_query, None, {}, None)

        self.set_job_group("", "")
        return self._return_dataframe_type(result_df, dataframe_type)

    def _sql_offline(self, sql_query, feature_store):
        # set feature store
        self._spark_session.sql("USE {}".format(feature_store))
        return self._spark_session.sql(sql_query)

    def show(self, sql_query, feature_store, n, online_conn):
        return self.sql(sql_query, feature_store, online_conn, "default", {}).show(n)

    def set_job_group(self, group_id, description):
        self._spark_session.sparkContext.setJobGroup(group_id, description)

    def register_on_demand_temporary_table(self, on_demand_fg, alias):
        on_demand_dataset = on_demand_fg.storage_connector.read(
            on_demand_fg.query,
            on_demand_fg.data_format,
            on_demand_fg.options,
            on_demand_fg.storage_connector._get_path(on_demand_fg.path),
        )
        if on_demand_fg.location:
            self._spark_session.sparkContext.textFile(on_demand_fg.location).collect()

        on_demand_dataset.createOrReplaceTempView(alias)
        return on_demand_dataset

    def register_hudi_temporary_table(
        self, hudi_fg_alias, feature_store_id, feature_store_name, read_options
    ):
        hudi_engine_instance = hudi_engine.HudiEngine(
            feature_store_id,
            feature_store_name,
            hudi_fg_alias.feature_group,
            self._spark_context,
            self._spark_session,
        )
        hudi_engine_instance.register_temporary_table(
            hudi_fg_alias.alias,
            hudi_fg_alias.left_feature_group_start_timestamp,
            hudi_fg_alias.left_feature_group_end_timestamp,
            read_options,
        )

    def _return_dataframe_type(self, dataframe, dataframe_type):
        if dataframe_type.lower() in ["default", "spark"]:
            return dataframe
        if dataframe_type.lower() == "pandas":
            return dataframe.toPandas()
        if dataframe_type.lower() == "numpy":
            return dataframe.toPandas().values
        if dataframe_type == "python":
            return dataframe.toPandas().values.tolist()

        raise TypeError(
            "Dataframe type `{}` not supported on this platform.".format(dataframe_type)
        )

    def convert_to_default_dataframe(self, dataframe):
        if isinstance(dataframe, pd.DataFrame):
            dataframe = self._spark_session.createDataFrame(dataframe)
        elif isinstance(dataframe, list):
            dataframe = np.array(dataframe)
        elif isinstance(dataframe, np.ndarray):
            if dataframe.ndim != 2:
                raise TypeError(
                    "Cannot convert numpy array that do not have two dimensions to a dataframe. "
                    "The number of dimensions are: {}".format(dataframe.ndim)
                )
            num_cols = dataframe.shape[1]
            dataframe_dict = {}
            for n_col in list(range(num_cols)):
                col_name = "col_" + str(n_col)
                dataframe_dict[col_name] = dataframe[:, n_col]
            pandas_df = pd.DataFrame(dataframe_dict)
            dataframe = self._spark_session.createDataFrame(pandas_df)
        elif isinstance(dataframe, RDD):
            dataframe = dataframe.toDF()

        if isinstance(dataframe, DataFrame):
            return dataframe.select(
                [col(x).alias(x.lower()) for x in dataframe.columns]
            )

        raise TypeError(
            "The provided dataframe type is not recognized. Supported types are: spark rdds, spark dataframes, "
            "pandas dataframes, python 2D lists, and numpy 2D arrays. The provided dataframe has type: {}".format(
                type(dataframe)
            )
        )

    def save_dataframe(
        self,
        feature_group,
        dataframe,
        operation,
        online_enabled,
        storage,
        offline_write_options,
        online_write_options,
        validation_id=None,
    ):
        try:
            if feature_group.stream:
                self._save_online_dataframe(
                    feature_group, dataframe, online_write_options
                )
            else:
                if storage == "offline" or not online_enabled:
                    self._save_offline_dataframe(
                        feature_group,
                        dataframe,
                        operation,
                        offline_write_options,
                        validation_id,
                    )
                elif storage == "online":
                    self._save_online_dataframe(
                        feature_group, dataframe, online_write_options
                    )
                elif online_enabled and storage is None:
                    self._save_offline_dataframe(
                        feature_group,
                        dataframe,
                        operation,
                        offline_write_options,
                    )
                    self._save_online_dataframe(
                        feature_group, dataframe, online_write_options
                    )
        except Exception:
            raise FeatureStoreException(
                "Error writing to offline and online feature store"
            )

    def save_stream_dataframe(
        self,
        feature_group,
        dataframe,
        query_name,
        output_mode,
        await_termination,
        timeout,
        checkpoint_dir,
        write_options,
    ):
        serialized_df = self._online_fg_to_avro(
            feature_group, self._encode_complex_features(feature_group, dataframe)
        )

        if query_name is None:
            query_name = "insert_stream_" + feature_group._online_topic_name

        query = (
            serialized_df.writeStream.outputMode(output_mode)
            .format(self.KAFKA_FORMAT)
            .option(
                "checkpointLocation",
                "/Projects/"
                + client.get_instance()._project_name
                + "/Resources/"
                + query_name
                + "-checkpoint"
                if checkpoint_dir is None
                else checkpoint_dir,
            )
            .options(**write_options)
            .option("topic", feature_group._online_topic_name)
            .queryName(query_name)
            .start()
        )

        if await_termination:
            query.awaitTermination(timeout)

        return query

    def _save_offline_dataframe(
        self,
        feature_group,
        dataframe,
        operation,
        write_options,
        validation_id=None,
    ):
        if feature_group.time_travel_format == "HUDI":
            hudi_engine_instance = hudi_engine.HudiEngine(
                feature_group.feature_store_id,
                feature_group.feature_store_name,
                feature_group,
                self._spark_session,
                self._spark_context,
            )
            hudi_engine_instance.save_hudi_fg(
                dataframe, self.APPEND, operation, write_options, validation_id
            )
        else:
            dataframe.write.format(self.HIVE_FORMAT).mode(self.APPEND).options(
                **write_options
            ).partitionBy(
                feature_group.partition_key if feature_group.partition_key else []
            ).saveAsTable(
                feature_group._get_table_name()
            )

    def _save_online_dataframe(self, feature_group, dataframe, write_options):

        serialized_df = self._online_fg_to_avro(
            feature_group, self._encode_complex_features(feature_group, dataframe)
        )
        serialized_df.write.format(self.KAFKA_FORMAT).options(**write_options).option(
            "topic", feature_group._online_topic_name
        ).save()

    def _encode_complex_features(self, feature_group, dataframe):
        """Encodes all complex type features to binary using their avro type as schema."""
        return dataframe.select(
            [
                field["name"]
                if field["name"] not in feature_group.get_complex_features()
                else to_avro(
                    field["name"], feature_group._get_feature_avro_schema(field["name"])
                ).alias(field["name"])
                for field in json.loads(feature_group.avro_schema)["fields"]
            ]
        )

    def _online_fg_to_avro(self, feature_group, dataframe):
        """Packs all features into named struct to be serialized to single avro/binary
        column. And packs primary key into arry to be serialized for partitioning.
        """
        return dataframe.select(
            [
                # be aware: primary_key array should always be sorted
                to_avro(
                    concat(
                        *[
                            col(f).cast("string")
                            for f in sorted(feature_group.primary_key)
                        ]
                    )
                ).alias("key"),
                to_avro(
                    struct(
                        [
                            field["name"]
                            for field in json.loads(feature_group.avro_schema)["fields"]
                        ]
                    ),
                    feature_group._get_encoded_avro_schema(),
                ).alias("value"),
            ]
        )

    def write_training_dataset(
        self, training_dataset, dataset, user_write_options, save_mode
    ):
        if isinstance(dataset, query.Query):
            dataset = dataset.read()

        dataset = self.convert_to_default_dataframe(dataset)

        if training_dataset.coalesce:
            dataset = dataset.coalesce(1)

        self.training_dataset_schema_match(dataset, training_dataset.schema)
        write_options = self.write_options(
            training_dataset.data_format, user_write_options
        )

        # check if there any transformation functions that require statistics attached to td features
        builtin_tffn_features = [
            ft_name
            for ft_name in training_dataset.transformation_functions
            if training_dataset._transformation_function_engine.is_builtin(
                training_dataset.transformation_functions[ft_name]
            )
        ]

        if len(training_dataset.splits) == 0:
            if builtin_tffn_features:
                # compute statistics before transformations are applied
                stats = training_dataset._statistics_engine.compute_transformation_fn_statistics(
                    td_metadata_instance=training_dataset,
                    columns=builtin_tffn_features,
                    feature_dataframe=dataset,
                )
                # Populate builtin transformations (if any) with respective arguments
                training_dataset._transformation_function_engine.populate_builtin_attached_fns(
                    training_dataset.transformation_functions, stats.content
                )
            # apply transformation functions (they are applied separately if there are splits)
            dataset = self._apply_transformation_function(training_dataset, dataset)

            path = training_dataset.location + "/" + training_dataset.name
            self._write_training_dataset_single(
                dataset,
                training_dataset.storage_connector,
                training_dataset.data_format,
                write_options,
                save_mode,
                path,
            )
        else:
            split_names = sorted([*training_dataset.splits])
            split_weights = [training_dataset.splits[i] for i in split_names]
            self._write_training_dataset_splits(
                training_dataset,
                dataset.randomSplit(split_weights, training_dataset.seed),
                write_options,
                save_mode,
                split_names,
                builtin_tffn_features=builtin_tffn_features,
            )

    def _write_training_dataset_splits(
        self,
        training_dataset,
        feature_dataframe_list,
        write_options,
        save_mode,
        split_names,
        builtin_tffn_features,
    ):
        stats = None
        if builtin_tffn_features:
            # compute statistics before transformations are applied
            i = [
                i
                for i, name in enumerate(split_names)
                if name == training_dataset.train_split
            ][0]
            stats = training_dataset._statistics_engine.compute_transformation_fn_statistics(
                td_metadata_instance=training_dataset,
                columns=builtin_tffn_features,
                feature_dataframe=feature_dataframe_list[i],
            )

        for i in range(len(feature_dataframe_list)):
            # Populate builtin transformations (if any) with respective arguments for each split
            if stats is not None:
                training_dataset._transformation_function_engine.populate_builtin_attached_fns(
                    training_dataset.transformation_functions, stats.content
                )
            # apply transformation functions (they are applied separately to each split)
            dataset = self._apply_transformation_function(
                training_dataset, dataset=feature_dataframe_list[i]
            )

            split_path = training_dataset.location + "/" + str(split_names[i])
            self._write_training_dataset_single(
                dataset,
                training_dataset.storage_connector,
                training_dataset.data_format,
                write_options,
                save_mode,
                split_path,
            )

    def _write_training_dataset_single(
        self,
        feature_dataframe,
        storage_connector,
        data_format,
        write_options,
        save_mode,
        path,
    ):
        # TODO: currently not supported petastorm, hdf5 and npy file formats
        if data_format.lower() == "tsv":
            data_format = "csv"

        path = self.setup_storage_connector(storage_connector, path)

        feature_dataframe.write.format(data_format).options(**write_options).mode(
            save_mode
        ).save(path)

    def read(self, storage_connector, data_format, read_options, location):
        if isinstance(location, str):
            if data_format.lower() in ["delta", "parquet", "hudi", "orc"]:
                # All the above data format readers can handle partitioning
                # by their own, they don't need /**
                path = location
            else:
                path = location + "/**"

            if data_format.lower() == "tsv":
                data_format = "csv"
        else:
            path = None

        path = self.setup_storage_connector(storage_connector, path)

        return (
            self._spark_session.read.format(data_format)
            .options(**(read_options if read_options else {}))
            .load(path)
        )

    def read_stream(
        self,
        storage_connector,
        message_format,
        schema,
        options,
        include_metadata,
    ):
        # ideally all this logic should be in the storage connector in case we add more
        # streaming storage connectors...
        stream = self._spark_session.readStream.format(storage_connector.SPARK_FORMAT)

        # set user options last so that they overwrite any default options
        stream = stream.options(**storage_connector.spark_options(), **options)

        if storage_connector.type == StorageConnector.KAFKA:
            return self._read_stream_kafka(
                stream, message_format, schema, include_metadata
            )

    def _read_stream_kafka(self, stream, message_format, schema, include_metadata):
        kafka_cols = [
            col("key"),
            col("topic"),
            col("partition"),
            col("offset"),
            col("timestamp"),
            col("timestampType"),
        ]

        if message_format == "avro" and schema is not None:
            # check if vallid avro schema
            avro.schema.parse(schema)
            df = stream.load()
            if include_metadata is True:
                return df.select(
                    *kafka_cols, from_avro(df.value, schema).alias("value")
                ).select(*kafka_cols, col("value.*"))
            return df.select(from_avro(df.value, schema).alias("value")).select(
                col("value.*")
            )
        elif message_format == "json" and schema is not None:
            df = stream.load()
            if include_metadata is True:
                return df.select(
                    *kafka_cols,
                    from_json(df.value.cast("string"), schema).alias("value")
                ).select(*kafka_cols, col("value.*"))
            return df.select(
                from_json(df.value.cast("string"), schema).alias("value")
            ).select(col("value.*"))

        if include_metadata is True:
            return stream.load()
        return stream.load().select("key", "value")

    def add_file(self, file):
        self._spark_context.addFile("hdfs://" + file)
        return SparkFiles.get(os.path.basename(file))

    def profile(
        self,
        dataframe,
        relevant_columns,
        correlations,
        histograms,
        exact_uniqueness=True,
    ):
        """Profile a dataframe with Deequ."""
        return (
            self._jvm.com.logicalclocks.hsfs.engine.SparkEngine.getInstance().profile(
                dataframe._jdf,
                relevant_columns,
                correlations,
                histograms,
                exact_uniqueness,
            )
        )

    def validate(self, dataframe, expectations, log_activity=True):
        """Run data validation on the dataframe with Deequ."""

        expectations_java = []
        for expectation in expectations:
            rules = []
            for rule in expectation.rules:
                rules.append(
                    self._jvm.com.logicalclocks.hsfs.metadata.validation.Rule.builder()
                    .name(
                        self._jvm.com.logicalclocks.hsfs.metadata.validation.RuleName.valueOf(
                            rule.get("name")
                        )
                    )
                    .level(
                        self._jvm.com.logicalclocks.hsfs.metadata.validation.Level.valueOf(
                            rule.get("level")
                        )
                    )
                    .min(rule.get("min", None))
                    .max(rule.get("max", None))
                    .pattern(rule.get("pattern", None))
                    .acceptedType(
                        self._jvm.com.logicalclocks.hsfs.metadata.validation.AcceptedType.valueOf(
                            rule.get("accepted_type")
                        )
                        if rule.get("accepted_type") is not None
                        else None
                    )
                    .feature((rule.get("feature", None)))
                    .legalValues(rule.get("legal_values", None))
                    .build()
                )
            expectation = (
                self._jvm.com.logicalclocks.hsfs.metadata.Expectation.builder()
                .name(expectation.name)
                .description(expectation.description)
                .features(expectation.features)
                .rules(rules)
                .build()
            )
            expectations_java.append(expectation)

        return self._jvm.com.logicalclocks.hsfs.engine.DataValidationEngine.getInstance().validate(
            dataframe._jdf, expectations_java
        )

    def write_options(self, data_format, provided_options):
        if data_format.lower() == "tfrecords":
            options = dict(recordType="Example")
            options.update(provided_options)
        elif data_format.lower() == "tfrecord":
            options = dict(recordType="Example")
            options.update(provided_options)
        elif data_format.lower() == "csv":
            options = dict(delimiter=",", header="true")
            options.update(provided_options)
        elif data_format.lower() == "tsv":
            options = dict(delimiter="\t", header="true")
            options.update(provided_options)
        else:
            options = {}
            options.update(provided_options)
        return options

    def read_options(self, data_format, provided_options):
        if data_format.lower() == "tfrecords":
            options = dict(recordType="Example", **provided_options)
            options.update(provided_options)
        elif data_format.lower() == "tfrecord":
            options = dict(recordType="Example")
            options.update(provided_options)
        elif data_format.lower() == "csv":
            options = dict(delimiter=",", header="true", inferSchema="true")
            options.update(provided_options)
        elif data_format.lower() == "tsv":
            options = dict(delimiter="\t", header="true", inferSchema="true")
            options.update(provided_options)
        else:
            options = {}
            options.update(provided_options)
        return options

    def parse_schema_feature_group(self, dataframe):
        return [
            feature.Feature(
                feat.name.lower(),
                feat.dataType.simpleString(),
                feat.metadata.get("description", None),
            )
            for feat in dataframe.schema
        ]

    def parse_schema_training_dataset(self, dataframe):
        return [
            training_dataset_feature.TrainingDatasetFeature(
                feat.name.lower(), feat.dataType.simpleString()
            )
            for feat in dataframe.schema
        ]

    def parse_schema_dict(self, dataframe):
        return {
            feat.name: feature.Feature(
                feat.name.lower(),
                feat.dataType.simpleString(),
                feat.metadata.get("description", ""),
            )
            for feat in dataframe.schema
        }

    def training_dataset_schema_match(self, dataframe, schema):
        schema_sorted = sorted(schema, key=lambda f: f.index)
        insert_schema = dataframe.schema
        if len(schema_sorted) != len(insert_schema):
            raise SchemaError(
                "Schemas do not match. Expected {} features, the dataframe contains {} features".format(
                    len(schema_sorted), len(insert_schema)
                )
            )

        i = 0
        for feat in schema_sorted:
            if feat.name != insert_schema[i].name.lower():
                raise SchemaError(
                    "Schemas do not match, expected feature {} in position {}, found {}".format(
                        feat.name, str(i), insert_schema[i].name
                    )
                )

            i += 1

    def setup_storage_connector(self, storage_connector, path=None):
        # update storage connector to get new session token
        storage_connector.refetch()

        if storage_connector.type == StorageConnector.S3:
            return self._setup_s3_hadoop_conf(storage_connector, path)
        elif storage_connector.type == StorageConnector.ADLS:
            return self._setup_adls_hadoop_conf(storage_connector, path)
        elif storage_connector.type == StorageConnector.GCS:
            return self._setup_gcp_hadoop_conf(storage_connector, path)
        else:
            return path

    def _setup_s3_hadoop_conf(self, storage_connector, path):
        if storage_connector.access_key:
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.access.key", storage_connector.access_key
            )
        if storage_connector.secret_key:
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.secret.key", storage_connector.secret_key
            )
        if storage_connector.server_encryption_algorithm:
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.server-side-encryption-algorithm",
                storage_connector.server_encryption_algorithm,
            )
        if storage_connector.server_encryption_key:
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.server-side-encryption-key",
                storage_connector.server_encryption_key,
            )
        if storage_connector.session_token:
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.aws.credentials.provider",
                "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider",
            )
            self._spark_context._jsc.hadoopConfiguration().set(
                "fs.s3a.session.token",
                storage_connector.session_token,
            )
        return path.replace("s3", "s3a", 1) if path is not None else None

    def _setup_adls_hadoop_conf(self, storage_connector, path):
        for k, v in storage_connector.spark_options().items():
            self._spark_context._jsc.hadoopConfiguration().set(k, v)

        return path

    def is_spark_dataframe(self, dataframe):
        if isinstance(dataframe, DataFrame):
            return True
        return False

    def get_empty_appended_dataframe(self, dataframe, new_features):
        dataframe = dataframe.limit(0)
        for f in new_features:
            dataframe = dataframe.withColumn(f.name, lit(None).cast(f.type))
        return dataframe

    def save_empty_dataframe(self, feature_group, dataframe):
        """Wrapper around save_dataframe in order to provide no-op in python engine."""
        self.save_dataframe(
            feature_group,
            dataframe,
            "upsert",
            feature_group.online_enabled,
            "offline",
            {},
            {},
        )

    def _apply_transformation_function(self, training_dataset, dataset):
        # generate transformation function expressions
        transformed_feature_names = []
        transformation_fn_expressions = []
        for (
            feature_name,
            transformation_fn,
        ) in training_dataset.transformation_functions.items():
            fn_registration_name = (
                transformation_fn.name + "_" + str(transformation_fn.version)
            )
            self._spark_session.udf.register(
                fn_registration_name, transformation_fn.transformation_fn
            )
            transformation_fn_expressions.append(
                "{fn_name:}({name:}) AS {name:}".format(
                    fn_name=fn_registration_name, name=feature_name
                )
            )
            transformed_feature_names.append(feature_name)

        # generate non transformation expressions
        no_transformation_expr = [
            "{name:} AS {name:}".format(name=col_name)
            for col_name in dataset.columns
            if col_name not in transformed_feature_names
        ]

        # generate entire expression and execute it
        transformation_fn_expressions.extend(no_transformation_expr)
        dataset = dataset.selectExpr(*transformation_fn_expressions)

        # sort feature order if it was altered by transformation functions
        sorded_features = sorted(training_dataset._features, key=lambda ft: ft.index)
        sorted_feature_names = [ft.name for ft in sorded_features]
        dataset = dataset.select(*sorted_feature_names)
        return dataset

    def _setup_gcp_hadoop_conf(self, storage_connector, path):

        PROPERTY_KEY_FILE = "fs.gs.auth.service.account.json.keyfile"
        PROPERTY_ENCRYPTION_KEY = "fs.gs.encryption.key"
        PROPERTY_ENCRYPTION_HASH = "fs.gs.encryption.key.hash"
        PROPERTY_ALGORITHM = "fs.gs.encryption.algorithm"
        PROPERTY_GCS_FS_KEY = "fs.AbstractFileSystem.gs.impl"
        PROPERTY_GCS_FS_VALUE = "com.google.cloud.hadoop.fs.gcs.GoogleHadoopFS"
        PROPERTY_GCS_ACCOUNT_ENABLE = "google.cloud.auth.service.account.enable"
        # The AbstractFileSystem for 'gs:' URIs
        self._spark_context._jsc.hadoopConfiguration().setIfUnset(
            PROPERTY_GCS_FS_KEY, PROPERTY_GCS_FS_VALUE
        )
        # Whether to use a service account for GCS authorization. Setting this
        # property to `false` will disable use of service accounts for authentication.
        self._spark_context._jsc.hadoopConfiguration().setIfUnset(
            PROPERTY_GCS_ACCOUNT_ENABLE, "true"
        )

        # The JSON key file of the service account used for GCS
        # access when google.cloud.auth.service.account.enable is true.
        local_path = self.add_file(storage_connector.key_path)
        self._spark_context._jsc.hadoopConfiguration().set(
            PROPERTY_KEY_FILE, local_path
        )

        if storage_connector.algorithm:
            # if encryption fields present
            self._spark_context._jsc.hadoopConfiguration().set(
                PROPERTY_ALGORITHM, storage_connector.algorithm
            )
            self._spark_context._jsc.hadoopConfiguration().set(
                PROPERTY_ENCRYPTION_KEY, storage_connector.encryption_key
            )
            self._spark_context._jsc.hadoopConfiguration().set(
                PROPERTY_ENCRYPTION_HASH, storage_connector.encryption_key_hash
            )
        else:
            # unset if already set
            self._spark_context._jsc.hadoopConfiguration().unset(PROPERTY_ALGORITHM)
            self._spark_context._jsc.hadoopConfiguration().unset(
                PROPERTY_ENCRYPTION_HASH
            )
            self._spark_context._jsc.hadoopConfiguration().unset(
                PROPERTY_ENCRYPTION_KEY
            )

        return path


class SchemaError(Exception):
    """Thrown when schemas don't match"""
