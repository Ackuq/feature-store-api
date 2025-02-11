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

import com.logicalclocks.hsfs.FeatureStoreException;
import lombok.SneakyThrows;
import org.apache.avro.Schema;
import org.apache.avro.SchemaParseException;
import org.apache.hudi.DataSourceUtils;
import org.apache.hudi.common.config.TypedProperties;
import org.apache.hudi.utilities.schema.SchemaProvider;
import org.apache.spark.api.java.JavaSparkContext;

import java.util.Collections;

public class DeltaStreamerSchemaProvider extends SchemaProvider {

  public DeltaStreamerSchemaProvider(TypedProperties props, JavaSparkContext jssc) {
    super(props, jssc);
    DataSourceUtils.checkRequiredProperties(props,
        Collections.singletonList(HudiEngine.FEATURE_GROUP_SCHEMA));
  }

  @SneakyThrows
  @Override
  public Schema getSourceSchema() {
    String featureGroupSchema = this.config.getString(HudiEngine.FEATURE_GROUP_SCHEMA);
    try {
      return new Schema.Parser().parse(featureGroupSchema);
    } catch (SchemaParseException e) {
      throw new FeatureStoreException("Failed to deserialize online feature group schema" + featureGroupSchema + ".");
    }
  }
}
