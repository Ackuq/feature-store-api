/*
 * Copyright (c) 2020 Logical Clocks AB
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

package com.logicalclocks.hsfs;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.logicalclocks.hsfs.engine.OnDemandFeatureGroupEngine;
import com.logicalclocks.hsfs.engine.CodeEngine;
import com.logicalclocks.hsfs.metadata.Expectation;
import com.logicalclocks.hsfs.metadata.FeatureGroupBase;
import com.logicalclocks.hsfs.metadata.OnDemandOptions;
import com.logicalclocks.hsfs.metadata.validation.ValidationType;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Getter;
import lombok.NonNull;
import lombok.Setter;
import org.apache.spark.sql.Dataset;
import org.apache.spark.sql.Row;
import scala.collection.JavaConverters;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@AllArgsConstructor
@JsonIgnoreProperties(ignoreUnknown = true)
public class OnDemandFeatureGroup extends FeatureGroupBase {

  @Getter
  @Setter
  private StorageConnector storageConnector;

  @Getter
  @Setter
  private String query;

  @Getter
  @Setter
  private OnDemandDataFormat dataFormat;

  @Getter
  @Setter
  private String path;

  @Getter
  @Setter
  private List<OnDemandOptions> options;

  @Getter
  @Setter
  private String type = "onDemandFeaturegroupDTO";

  private OnDemandFeatureGroupEngine onDemandFeatureGroupEngine = new OnDemandFeatureGroupEngine();
  private final CodeEngine codeEngine = new CodeEngine(EntityEndpointType.FEATURE_GROUP);

  @Builder
  public OnDemandFeatureGroup(FeatureStore featureStore, @NonNull String name, Integer version, String query,
                              OnDemandDataFormat dataFormat, String path, Map<String, String> options,
                              @NonNull StorageConnector storageConnector, String description, List<String> primaryKeys,
                              List<Feature> features, StatisticsConfig statisticsConfig,
                              scala.collection.Seq<Expectation> expectations,
                              ValidationType validationType, String eventTime) {
    this.featureStore = featureStore;
    this.name = name;
    this.version = version;
    this.query = query;
    this.dataFormat = dataFormat;
    this.path = path;
    this.options = options != null ? options.entrySet().stream()
        .map(e -> new OnDemandOptions(e.getKey(), e.getValue()))
        .collect(Collectors.toList())
        : null;
    this.description = description;
    this.primaryKeys = primaryKeys != null
            ? primaryKeys.stream().map(String::toLowerCase).collect(Collectors.toList()) : null;
    this.storageConnector = storageConnector;
    this.features = features;
    this.statisticsConfig = statisticsConfig != null ? statisticsConfig : new StatisticsConfig();
    this.eventTime = eventTime;
    this.validationType = validationType != null ? validationType : ValidationType.NONE;
    if (expectations != null && !expectations.isEmpty()) {
      this.expectationsNames = new ArrayList<>();
      this.expectations = JavaConverters.seqAsJavaListConverter(expectations).asJava();
      this.expectations.forEach(expectation -> this.expectationsNames.add(expectation.getName()));
    }
  }

  public OnDemandFeatureGroup() {
  }

  public OnDemandFeatureGroup(FeatureStore featureStore, int id) {
    this.featureStore = featureStore;
    this.id = id;
  }

  public void save() throws FeatureStoreException, IOException {
    onDemandFeatureGroupEngine.saveFeatureGroup(this);
    codeEngine.saveCode(this);
    if (statisticsConfig.getEnabled()) {
      statisticsEngine.computeStatistics(this, read(), null);
    }
  }

  @Override
  public Dataset<Row> read() throws FeatureStoreException, IOException {
    return (Dataset<Row>) selectAll().read();
  }

  public void show(int numRows) throws FeatureStoreException, IOException {
    read().show(numRows);
  }
}
