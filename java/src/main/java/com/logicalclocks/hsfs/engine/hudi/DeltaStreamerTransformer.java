/*
 * Copyright (c) 2021 Logical Clocks AB
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *
 * See the License for the specific language governing permissions and limitations under the License.
 */

package com.logicalclocks.hsfs.engine.hudi;

import com.logicalclocks.hsfs.FeatureStore;
import com.logicalclocks.hsfs.StreamFeatureGroup;
import com.logicalclocks.hsfs.engine.FeatureGroupUtils;
import com.logicalclocks.hsfs.metadata.FeatureGroupApi;
import com.logicalclocks.hsfs.metadata.FeatureStoreApi;
import lombok.SneakyThrows;
import org.apache.hudi.common.config.TypedProperties;
import org.apache.hudi.utilities.transform.Transformer;
import org.apache.spark.api.java.JavaSparkContext;
import org.apache.spark.sql.Dataset;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

public class DeltaStreamerTransformer implements Transformer {
  private FeatureStore featureStore;
  private StreamFeatureGroup featureGroup;

  private final FeatureStoreApi featureStoreApi = new FeatureStoreApi();
  private final FeatureGroupApi featureGroupApi = new FeatureGroupApi();
  private FeatureGroupUtils utils = new FeatureGroupUtils();

  public DeltaStreamerTransformer() {
  }

  @SneakyThrows
  @Override
  public Dataset<Row> apply(JavaSparkContext javaSparkContext, SparkSession sparkSession, Dataset<Row> dataset,
                            TypedProperties props) {
    featureStore = featureStoreApi.get(Integer.parseInt(props.getString("projectId")),
        props.getString("featureStoreName"));
    featureGroup = featureGroupApi.getStreamFeatureGroup(this.featureStore, props.getString("featureGroupName"),
        Integer.parseInt(props.getString("featureGroupVersion")));

    return utils.sanitizeFeatureNames(dataset);
  }
}
